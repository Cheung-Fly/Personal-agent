"""上下文管理（v0.6）测试：不依赖 LLM。 python -m pytest tests/ -q"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("WORKSPACE_DIR", "./agent_workspace_test")

from backend.context import (  # noqa: E402
    apply_budget,
    estimate_tokens,
    message_tokens,
    truncate_history,
)


def _round(i: int, pad: int = 40) -> list[dict]:
    """一轮 user/assistant，每条消息约 pad+4 个英文字符 ≈ (pad+4)/4 token。"""
    return [{"role": "user", "content": f"问题{i}：" + "a" * pad},
            {"role": "assistant", "content": f"回答{i}：" + "b" * pad}]


def _history(n: int, pad: int = 40) -> list[dict]:
    out: list[dict] = []
    for i in range(1, n + 1):
        out.extend(_round(i, pad))
    return out


# ---- 估算：中英文混合 ----

def test_estimate_empty_and_single():
    assert estimate_tokens("") == 0
    assert estimate_tokens("a") == 1  # 非空至少 1
    assert estimate_tokens("你") == 1


def test_estimate_english():
    assert estimate_tokens("abcd") == 1  # 4 字符 = 1 token
    assert estimate_tokens("hello world") == 3  # 11 字符 / 4，向上取整


def test_estimate_chinese():
    assert estimate_tokens("你好世界") == 3  # 4 字符 / 1.75，向上取整
    assert estimate_tokens("你好") == 2  # 2 / 1.75 = 1.15 -> 2


def test_estimate_mixed():
    # "hello" 5 字符 / 4 + "你好" 2 字符 / 1.75 = 1.25 + 1.14 -> 3
    assert estimate_tokens("hello你好") == 3


def test_message_tokens_empty():
    assert message_tokens({"role": "user", "content": ""}) == 0
    assert message_tokens({"role": "assistant", "content": None}) == 0
    assert message_tokens({"role": "user", "content": "abcd"}) == 1


# ---- 截断：丢最旧、保最新 ----

def test_truncate_under_budget_no_drop():
    history = _history(3)
    kept, dropped = truncate_history(history, budget=10000)
    assert kept == history and dropped == 0


def test_truncate_drops_oldest_rounds():
    # 每轮约 (44/4)*2 = 22 token；5 轮约 110 token，预算 50：
    # 丢第 1 轮（剩 88）-> 丢第 2 轮（剩 66）-> 丢第 3 轮（剩 44， fits）
    history = _history(5)
    kept, dropped = truncate_history(history, budget=50)
    assert dropped == 3
    assert len(kept) == 4  # 只剩第 4、5 轮
    assert kept[0]["content"].startswith("问题4")
    assert kept[-1]["content"].startswith("回答5")


def test_truncate_never_drops_latest_round():
    # 最新一轮自己就超预算，也不能丢——丢了等于丢了用户刚说的话。
    history = _round(1, pad=10) + _round(2, pad=800)
    kept, dropped = truncate_history(history, budget=50)
    assert dropped == 1
    assert kept == _round(2, pad=800)


def test_truncate_single_round_never_dropped():
    history = _round(1, pad=800)
    kept, dropped = truncate_history(history, budget=10)
    assert kept == history and dropped == 0


def test_truncate_counts_memory_tokens():
    # 2 轮约 44 token + memory 20 token = 64 > 预算 50，丢一轮后 42 fits
    history = _history(2)
    kept, dropped = truncate_history(history, budget=50, memory_tokens=20)
    assert dropped == 1 and len(kept) == 2


# ---- 组装级截断：system / memory / 本轮 user 永不丢 ----

def test_apply_budget_keeps_system_and_user():
    messages = ([{"role": "system", "content": "sys"}]
                + _history(5)
                + [{"role": "user", "content": "现在"}])
    out, stats = apply_budget(messages, budget=50)
    assert stats["dropped_rounds"] == 3
    assert out[0]["content"] == "sys"  # system 永不丢
    assert out[-1]["content"] == "现在"  # 本轮 user 永不丢
    assert out[1]["content"].startswith("问题4")
    # context_tokens 是预算口径：截断后 history 的估算值（不含 system/本轮 user）
    assert stats["context_tokens"] == sum(message_tokens(m) for m in out[1:-1])


def test_apply_budget_keeps_memory():
    memory = {"role": "system", "content": "偏好：回复简洁。"}
    messages = ([{"role": "system", "content": "sys"}, memory]
                + _history(5)
                + [{"role": "user", "content": "现在"}])
    out, stats = apply_budget(messages, budget=50)
    assert out[1] is memory  # memory 是第二条 system 消息，永不丢
    assert stats["context_tokens"] >= message_tokens(memory)
    assert out[-1]["content"] == "现在"


def test_apply_budget_empty_history():
    messages = [{"role": "system", "content": "sys"},
                {"role": "user", "content": "你好"}]
    out, stats = apply_budget(messages, budget=10)
    assert out == messages and stats == {"context_tokens": 0, "dropped_rounds": 0}


# ---- 接入 _build_messages：流式与非流式共用同一逻辑 ----

def test_build_messages_with_budget(monkeypatch):
    monkeypatch.setattr("backend.context.CONTEXT_TOKEN_BUDGET", 50)
    from backend.main import ChatIn, _build_messages_with_budget

    body = ChatIn(message="现在", history=_history(5))
    msgs, stats = _build_messages_with_budget(body, "unused-session-id")
    assert stats["dropped_rounds"] == 3
    assert [m["role"] for m in msgs] == ["system", "user", "assistant",
                                         "user", "assistant", "user"]
    assert msgs[1]["content"].startswith("问题4")
    assert msgs[-1]["content"] == "现在"


def test_build_messages_with_budget_keeps_memory(monkeypatch, tmp_path):
    # 预算 100：5 轮 history 约 110 token + memory 约 48 token（含 header），
    # 不计 memory 只丢 1 轮，计入 memory 要丢 3 轮——memory 参与计数但永不丢。
    monkeypatch.setattr("backend.context.CONTEXT_TOKEN_BUDGET", 100)
    monkeypatch.setattr("backend.memory.WORKSPACE", tmp_path)
    (tmp_path / "MEMORY.md").write_text("偏好：回复简洁。", encoding="utf-8")
    from backend.main import ChatIn, _build_messages_with_budget

    body = ChatIn(message="现在", history=_history(5))
    msgs, stats = _build_messages_with_budget(body, "unused-session-id")
    assert msgs[1]["role"] == "system" and "偏好：回复简洁。" in msgs[1]["content"]
    assert stats["dropped_rounds"] == 3
    assert stats["context_tokens"] == message_tokens(msgs[1]) + sum(
        message_tokens(m) for m in msgs[2:-1])
    assert msgs[-1]["content"] == "现在"


def test_build_messages_default_budget_no_drop(monkeypatch):
    # 默认 8000 预算下，几轮短历史一轮不丢。
    from backend.main import ChatIn, _build_messages_with_budget

    body = ChatIn(message="现在", history=_history(5))
    _, stats = _build_messages_with_budget(body, "unused-session-id")
    assert stats["dropped_rounds"] == 0
