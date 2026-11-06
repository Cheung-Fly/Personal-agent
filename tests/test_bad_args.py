"""v0.14.1 工具参数解析护法（GitHub Issue #1）单测。

背景：模型输出被截断等情况下，tool_calls 的 arguments 可能是坏
JSON（非法字符串 / 截断）或合法 JSON 但非 dict（数组等）。旧代码
在流式 stream_agent 与非流式 /api/chat 两条循环里裸 json.loads，
异常逃逸：流式生成器崩掉（无 done/error 事件、用户消息已落库却
没回复），非流式直接 500。修复口径：解析护住（含非 dict 检查），
失败不执行、按 tool_call_id 回喂 BAD_ARGS_TEXT、记审计
outcome=bad_arguments，循环继续、模型可自纠重试。

这里验的是机制：
- 解析 helper：三形态坏参数 → None；正常 dict / 空参 → dict；
- 两条循环各覆盖三形态：不抛异常、循环不中断、坏调用收到失败
  消息、同轮的正常调用照常执行、最终正常收尾（非流式 200 而非
  500；流式照常走到 done 事件）；
- 流式与 v0.12 护栏的账本关系：坏参数的 write_file 记空 dict
  占位，不冒充“写过 MEMORY.md”，不让护栏判定出错——读过记忆、
  写入坏参失败后纯文本收尾，护栏仍恰好推一次。
"""
import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("WORKSPACE_DIR", "./agent_workspace_test")

# 三形态坏参数：非法字符串 / 截断的 JSON / 合法 JSON 但不是 dict。
BAD_INVALID = "not-a-json{{{"
BAD_TRUNCATED = '{"path": "note.txt", "con'
BAD_NONDICT = '["note.txt"]'
BAD_SHAPES = [BAD_INVALID, BAD_TRUNCATED, BAD_NONDICT]


# ---------------- 解析 helper ----------------

def test_parse_tool_args():
    from backend.main import _parse_tool_args

    assert _parse_tool_args('{"path": "a.py"}') == {"path": "a.py"}
    assert _parse_tool_args("") == {}       # 空参等同无参
    assert _parse_tool_args(None) == {}
    for bad in BAD_SHAPES:
        assert _parse_tool_args(bad) is None
    assert _parse_tool_args('"just a string"') is None
    assert _parse_tool_args("42") is None


def _audit_rows(tmp_path, outcome):
    p = tmp_path / "data" / "audit.log"
    if not p.exists():
        return []
    return [json.loads(line)
            for line in p.read_text(encoding="utf-8").splitlines()
            if f'"outcome": "{outcome}"' in line]


# ---------------- 非流式 /api/chat ----------------

def _run_chat(tmp_path, monkeypatch, raw_bad_args):
    """脚本化假 LLM：第 1 轮同发一个坏参调用 + 一个正常调用，
    第 2 轮纯文本收尾。返回 (响应 JSON, 第 2 轮收到的 messages)。"""
    monkeypatch.setattr("backend.main.WORKSPACE", tmp_path)
    monkeypatch.setattr("backend.tools.WORKSPACE", tmp_path)
    monkeypatch.setattr("backend.memory.WORKSPACE", tmp_path)
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    (tmp_path / "note.txt").write_text("正常文件内容", encoding="utf-8")

    from fastapi.testclient import TestClient

    import backend.main as main

    seen = {}
    calls = {"n": 0}

    async def fake_llm(messages):
        calls["n"] += 1
        usage = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
        if calls["n"] == 1:
            return {"tool_calls": [
                {"id": "bad-1", "function": {"name": "read_file",
                                             "arguments": raw_bad_args}},
                {"id": "good-1", "function": {"name": "read_file",
                                              "arguments": '{"path": "note.txt"}'}},
            ]}, usage
        seen["messages"] = [dict(m) for m in messages]
        return {"content": "已处理"}, usage

    monkeypatch.setattr("backend.main.call_llm_with_usage", fake_llm)
    monkeypatch.setattr("backend.main.LLM_API_KEY", "test-key")
    client = TestClient(main.app)
    r = client.post("/api/chat", json={"message": "读一下 note.txt"})
    assert r.status_code == 200  # 旧行为是 500
    return main, r.json(), seen["messages"]


@pytest.mark.parametrize("raw_bad_args", BAD_SHAPES)
def test_nonstreaming_bad_args_fed_back_not_500(tmp_path, monkeypatch, raw_bad_args):
    main, payload, messages = _run_chat(tmp_path, monkeypatch, raw_bad_args)
    assert payload["reply"] == "已处理"  # 循环没中断，走到了收尾
    tool_log = payload["tool_log"]
    assert len(tool_log) == 2
    assert tool_log[0]["result"] == main.BAD_ARGS_TEXT      # 坏调用：失败文本
    assert "正常文件内容" in tool_log[1]["result"]          # 正常调用：照常执行
    # 回喂给模型的 tool 消息按 tool_call_id 对齐。
    tool_msgs = {m["tool_call_id"]: m["content"]
                 for m in messages if m.get("role") == "tool"}
    assert tool_msgs["bad-1"] == main.BAD_ARGS_TEXT
    assert "正常文件内容" in tool_msgs["good-1"]
    # 审计留痕：bad_arguments 一行，工具没被执行（没有 auto 行给坏调用）。
    rows = _audit_rows(tmp_path, "bad_arguments")
    assert len(rows) == 1 and rows[0]["tool"] == "read_file"


# ---------------- 流式 stream_agent（脚本化假流式 LLM） ----------------

class _FakeStreamResponse:
    def __init__(self, lines):
        self._lines = lines

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def raise_for_status(self):
        pass

    async def aiter_lines(self):
        for line in self._lines:
            yield line


class _ScriptedClient:
    """假 httpx.AsyncClient：按 scripts 逐请求返回流式 chunk，
    并记录每个请求的 payload 供断言回喂内容。"""
    scripts: list = []
    payloads: list = []

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def stream(self, method, url, headers=None, json=None):
        type(self).payloads.append(json)
        idx = len(type(self).payloads) - 1
        return _FakeStreamResponse(type(self).scripts[idx])


def _data(obj):
    return "data: " + json.dumps(obj, ensure_ascii=False)


def _text_lines(text):
    return [_data({"choices": [{"delta": {"content": text}}]}), "data: [DONE]"]


def _raw_tool_lines(calls):
    """calls: [(index, call_id, name, [参数原文分片...])] → SSE 行。
    分片按流式 delta 逐片到达，循环层要先拼完整再解析——截断形态
    正是从这里拼出坏 JSON 的。"""
    lines = []
    for idx, call_id, name, fragments in calls:
        for j, frag in enumerate(fragments):
            fn = {"arguments": frag}
            tc = {"index": idx, "function": fn}
            if j == 0:
                fn["name"] = name
                tc["id"] = call_id
            lines.append(_data({"choices": [{"delta": {"tool_calls": [tc]}}]}))
    lines.append("data: [DONE]")
    return lines


def _run_stream(tmp_path, monkeypatch, *, scripts, message):
    import backend.main as main
    from backend.storage import ensure_session

    monkeypatch.setattr("backend.main.WORKSPACE", tmp_path)
    monkeypatch.setattr("backend.tools.WORKSPACE", tmp_path)
    monkeypatch.setattr("backend.memory.WORKSPACE", tmp_path)
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    _ScriptedClient.scripts = scripts
    _ScriptedClient.payloads = []
    monkeypatch.setattr("backend.main.httpx.AsyncClient", _ScriptedClient)

    session_id = ensure_session(None, title_hint=message)
    body = main.ChatIn(message=message, session_id=session_id)

    async def drive():
        events = []
        async for raw in main.stream_agent(body, session_id):
            events.append(json.loads(raw[len("data: "):]))
        return events

    return main, asyncio.run(drive())


@pytest.mark.parametrize("raw_bad_args", BAD_SHAPES)
def test_streaming_bad_args_fed_back_and_done(tmp_path, monkeypatch, raw_bad_args):
    (tmp_path / "note.txt").write_text("正常文件内容", encoding="utf-8")
    # 坏参分片到达（截断形态正是这样拼坏的），正常调用同轮随后。
    fragments = [raw_bad_args[:8], raw_bad_args[8:]]
    scripts = [
        _raw_tool_lines([
            (0, "bad-1", "read_file", fragments),
            (1, "good-1", "read_file", ['{"path": "note.txt"}']),
        ]),
        _text_lines("已处理"),
    ]
    main, events = _run_stream(tmp_path, monkeypatch,
                               scripts=scripts, message="读一下 note.txt")
    types = [e["type"] for e in events]
    assert types[-1] == "done"  # 旧行为：生成器崩掉，永远到不了 done
    assert events[-1]["reply"] == "已处理"
    bad = [e for e in events if e["type"] == "tool_result"
           and e["result"] == main.BAD_ARGS_TEXT]
    good = [e for e in events if e["type"] == "tool_result"
            and "正常文件内容" in e["result"]]
    assert len(bad) == 1 and len(good) == 1  # 坏的回失败、好的照常执行
    # 第 2 轮请求里，两条 tool 消息按 tool_call_id 对齐回喂。
    second = _ScriptedClient.payloads[1]
    tool_msgs = {m["tool_call_id"]: m["content"]
                 for m in second["messages"] if m.get("role") == "tool"}
    assert tool_msgs["bad-1"] == main.BAD_ARGS_TEXT
    assert "正常文件内容" in tool_msgs["good-1"]
    rows = _audit_rows(tmp_path, "bad_arguments")
    assert len(rows) == 1 and rows[0]["tool"] == "read_file"


def test_streaming_bad_write_does_not_fool_guardrail(tmp_path, monkeypatch):
    """护栏账本：坏参 write_file 记空 dict 占位——不冒充写过
    MEMORY.md、判定不崩；读过记忆 + 写入坏参失败 + 纯文本收尾时，
    护栏仍恰好推一次，之后如实收尾到 done。"""
    original = "# 长期记忆\n\n- 回复默认用中文，简洁直接。\n"
    (tmp_path / "MEMORY.md").write_text(original, encoding="utf-8")
    scripts = [
        _raw_tool_lines([(0, "c1", "read_file", ['{"path": "MEMORY.md"}'])]),
        _raw_tool_lines([(0, "c2", "write_file", [BAD_TRUNCATED])]),
        _text_lines("好的，我拟好了要写的内容。"),   # 纯文本收尾 → 护栏应推
        _text_lines("想好了，还是先不写了。"),        # 推过后如实收尾
    ]
    message = "请把这条偏好写进我的长期记忆（写进 MEMORY.md）：我的幸运数字是 7391。"
    main, events = _run_stream(tmp_path, monkeypatch,
                               scripts=scripts, message=message)
    assert events[-1]["type"] == "done"
    assert events[-1]["reply"] == "想好了，还是先不写了。"
    # 坏参写入根本没执行：文件一字未动；失败回喂留了审计。
    assert (tmp_path / "MEMORY.md").read_text(encoding="utf-8") == original
    assert len(_audit_rows(tmp_path, "bad_arguments")) == 1
    # 护栏恰好推一次（坏参 write 没被误记成“发过 write_file”）。
    assert len(_audit_rows(tmp_path, "memory_write_nudge")) == 1
