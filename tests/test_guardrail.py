"""v0.12 长期记忆写入护栏（循环层）单测。

背景：t09 四轮 live 样本败/过/过/败——模型读完 MEMORY.md、用户也明说
要写，却以纯文本收尾，write_file 没发出。v0.9 的提示词句被证伪为概率
性改善，本版在流式循环收尾分支加护栏：三条件（明确写入意图 + 本请求
读过 MEMORY.md + 没发过 write_file）齐备、且每请求没推过、且还有后续
轮次时，注入一条提醒续跑一轮。

这里验的是机制，不是模型表现：
- 判定函数矩阵：三条件缺一不推（只读提问尤其不能误推）；
- 循环行为（脚本化假流式 LLM 驱动 stream_agent）：推且只推一次、
  第二次纯文本收尾如实返回、被推出来的 write_file 仍走审批卡
  （卡点前文件不动、点通过才落盘）、最后一轮不推（不延长 MAX_ROUNDS）。
"""
import asyncio
import json


# ---------------- 纯判定函数 ----------------
# 注意：backend.main 只在函数内导入（v0.9 教训：顶层导入 backend.main
# 会在收集期提前绑死 WORKSPACE 引用，干扰 test_approval 的 monkeypatch）。

def test_intent_requires_verb_and_memory_noun():
    from backend.main import _memory_write_intent

    # t09 任务原话（评测原文，一字不差的子集）与常见同类表述 → 算意图。
    assert _memory_write_intent("请把这条偏好写进我的长期记忆（写进 MEMORY.md）：我的幸运数字是 7391。")
    assert _memory_write_intent("把我的幸运数字写入 MEMORY.md")
    assert _memory_write_intent("帮我记到长期记忆里：我喝咖啡不加糖")
    # 只读提问：没有写入动词 → 不算（误推防线第一关）。
    assert not _memory_write_intent("我的幸运数字是多少？")
    assert not _memory_write_intent("我的偏好是什么")
    assert not _memory_write_intent("讲讲长期记忆是怎么实现的")
    # 写别的文件：没有记忆类名词 → 不算。
    assert not _memory_write_intent("把这段说明写进 README.md")
    assert not _memory_write_intent("")


def test_is_memory_path():
    from backend.main import _is_memory_path

    assert _is_memory_path({"path": "MEMORY.md"})
    assert _is_memory_path({"path": "./MEMORY.md"})
    assert not _is_memory_path({"path": "docs/MEMORY.md"})  # 只认根目录那个
    assert not _is_memory_path({"path": "OTHER.md"})
    assert not _is_memory_path({})
    assert not _is_memory_path(None)


def test_nudge_needed_matrix():
    from backend.main import _memory_nudge_needed

    msg = "请把这条偏好写进我的长期记忆（写进 MEMORY.md）：我的幸运数字是 7391。"
    read_mem = ("read_file", {"path": "MEMORY.md"})
    write_mem = ("write_file", {"path": "MEMORY.md", "content": "x"})
    read_other = ("read_file", {"path": "docs/guide.md"})

    assert _memory_nudge_needed(msg, [read_mem])                    # 三条件齐备
    assert not _memory_nudge_needed(msg, [read_mem, write_mem])     # 发过 write（成败不论）
    assert not _memory_nudge_needed(msg, [])                        # 没读过
    assert not _memory_nudge_needed(msg, [read_other])              # 读的不是 MEMORY.md
    assert not _memory_nudge_needed("我的幸运数字是多少？", [read_mem])  # 只读提问
    assert _memory_nudge_needed(msg, [("write_file", {"path": "a.py"}), read_mem])  # 写别的文件不影响


# ---------------- 循环行为：脚本化假流式 LLM ----------------

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
    并把每个请求的 payload 记下来供断言（看护栏提醒是否进了上下文）。"""
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


def _tool_lines(name, args, call_id="call-1"):
    delta = {"tool_calls": [{"index": 0, "id": call_id, "function": {
        "name": name, "arguments": json.dumps(args, ensure_ascii=False)}}]}
    return [_data({"choices": [{"delta": delta}]}), "data: [DONE]"]


_ORIGINAL_MEMORY = "# 长期记忆\n\n- 回复默认用中文，简洁直接。\n"
_T09_MESSAGE = "请把这条偏好写进我的长期记忆（写进 MEMORY.md）：我的幸运数字是 7391。"


def _nudge_rows(tmp_path):
    p = tmp_path / "data" / "audit.log"
    if not p.exists():
        return []
    return [json.loads(line)
            for line in p.read_text(encoding="utf-8").splitlines()
            if '"memory_write_nudge"' in line]


def _run_stream(tmp_path, monkeypatch, *, scripts, message, max_rounds=None):
    """把 stream_agent 在临时工作区/数据目录里驱动完，审批卡一律点通过。
    返回 (main 模块, events 列表, 审批卡出现那一刻的 MEMORY.md 内容或 None)。"""
    import backend.main as main
    from backend.storage import ensure_session

    monkeypatch.setattr("backend.main.WORKSPACE", tmp_path)
    monkeypatch.setattr("backend.tools.WORKSPACE", tmp_path)
    monkeypatch.setattr("backend.memory.WORKSPACE", tmp_path)
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    if max_rounds is not None:
        monkeypatch.setattr("backend.main.MAX_ROUNDS", max_rounds)
    (tmp_path / "MEMORY.md").write_text(_ORIGINAL_MEMORY, encoding="utf-8")
    _ScriptedClient.scripts = scripts
    _ScriptedClient.payloads = []
    monkeypatch.setattr("backend.main.httpx.AsyncClient", _ScriptedClient)

    session_id = ensure_session(None, title_hint=message)
    body = main.ChatIn(message=message, session_id=session_id)
    seen = {"memory_at_card": None}

    async def drive():
        events = []
        async for raw in main.stream_agent(body, session_id):
            ev = json.loads(raw[len("data: "):])
            events.append(ev)
            if ev.get("type") == "approval_required":
                # 卡点这一刻先看文件：审批通过前绝不能已被写。
                seen["memory_at_card"] = (tmp_path / "MEMORY.md").read_text(encoding="utf-8")
                main.approvals.decide(ev["approval_id"], "approve")
        return events

    events = asyncio.run(drive())
    return main, events, seen["memory_at_card"]


def test_stream_nudge_then_write_still_goes_through_approval(tmp_path, monkeypatch):
    """失败形态复现 → 护栏推一次 → 模型改发 write_file → 仍走审批卡。"""
    new_memory = "# 长期记忆\n\n- 回复默认用中文，简洁直接。\n- 我的幸运数字是 7391。\n"
    scripts = [
        _tool_lines("read_file", {"path": "MEMORY.md"}),
        _text_lines("好的，我拟好了要写的内容：你的幸运数字是 7391。"),  # 纯文本收尾 = t09 失败形态
        _tool_lines("write_file", {"path": "MEMORY.md", "content": new_memory}, call_id="call-2"),
        _text_lines("已记好了。"),
    ]
    main, events, at_card = _run_stream(tmp_path, monkeypatch,
                                        scripts=scripts, message=_T09_MESSAGE)
    types = [e["type"] for e in events]
    assert types[-1] == "done"
    assert events[-1]["reply"] == "已记好了。"
    # 护栏恰好触发一次，且留了审计行。
    assert len(_nudge_rows(tmp_path)) == 1
    # 提醒确实进了下一轮的请求上下文（第 3 个请求 = 被推后的那轮）。
    third = _ScriptedClient.payloads[2]
    assert any(m.get("role") == "user" and m.get("content") == main.MEMORY_NUDGE_TEXT
               for m in third["messages"])
    # write_file 走了审批卡：卡出现时文件还是原样，通过后才落盘。
    cards = [e for e in events if e["type"] == "approval_required"]
    assert len(cards) == 1 and cards[0]["tool"] == "write_file"
    assert at_card == _ORIGINAL_MEMORY
    assert (tmp_path / "MEMORY.md").read_text(encoding="utf-8") == new_memory


def test_stream_nudge_only_once_then_honest_finish(tmp_path, monkeypatch):
    """推过一次后模型仍纯文本收尾 → 如实返回，不再推、系统不代写。"""
    scripts = [
        _tool_lines("read_file", {"path": "MEMORY.md"}),
        _text_lines("我拟好了内容，先不发调用。"),
        _text_lines("想了想还是只给你看内容，不写了。"),
    ]
    main, events, _ = _run_stream(tmp_path, monkeypatch,
                                  scripts=scripts, message=_T09_MESSAGE)
    assert events[-1]["type"] == "done"
    assert events[-1]["reply"] == "想了想还是只给你看内容，不写了。"
    assert len(_nudge_rows(tmp_path)) == 1
    assert not any(e["type"] == "approval_required" for e in events)
    assert (tmp_path / "MEMORY.md").read_text(encoding="utf-8") == _ORIGINAL_MEMORY


def test_stream_readonly_question_no_nudge(tmp_path, monkeypatch):
    """只读提问读了 MEMORY.md 回答问题：意图条件不满足，绝不能误推。"""
    scripts = [
        _tool_lines("read_file", {"path": "MEMORY.md"}),
        _text_lines("你的幸运数字我还没记，先告诉我吧。"),
    ]
    main, events, _ = _run_stream(
        tmp_path, monkeypatch, scripts=scripts,
        message="我的幸运数字是多少？先读一下长期记忆再回答。")
    assert events[-1]["type"] == "done"
    assert len(_ScriptedClient.payloads) == 2  # 没被多推一轮
    assert _nudge_rows(tmp_path) == []


def test_stream_direct_write_no_nudge(tmp_path, monkeypatch):
    """模型没读直接就写（mock 链路里 t09 的走法）：护栏不介入。"""
    scripts = [
        _tool_lines("write_file", {"path": "MEMORY.md", "content": _ORIGINAL_MEMORY}),
        _text_lines("已存好了。"),
    ]
    main, events, _ = _run_stream(tmp_path, monkeypatch,
                                  scripts=scripts, message=_T09_MESSAGE)
    assert events[-1]["reply"] == "已存好了。"
    assert _nudge_rows(tmp_path) == []


def test_stream_no_nudge_on_last_round(tmp_path, monkeypatch):
    """只剩最后一轮时不推：护栏不能延长 MAX_ROUNDS 预算。"""
    scripts = [
        _tool_lines("read_file", {"path": "MEMORY.md"}),
        _text_lines("拟好了内容。"),
    ]
    main, events, _ = _run_stream(tmp_path, monkeypatch, scripts=scripts,
                                  message=_T09_MESSAGE, max_rounds=2)
    assert events[-1]["type"] == "done"
    assert events[-1]["reply"] == "拟好了内容。"
    assert _nudge_rows(tmp_path) == []
