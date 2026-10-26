"""长期记忆（v0.4）测试：不依赖 LLM。 python -m pytest tests/ -q"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("WORKSPACE_DIR", "./agent_workspace_test")

from backend.memory import (  # noqa: E402
    MEMORY_MAX_CHARS,
    load_long_term_memory,
    memory_system_message,
)
from backend.tools import run_tool  # noqa: E402


def test_missing_file_returns_empty(tmp_path):
    assert load_long_term_memory(tmp_path) == ""
    assert memory_system_message(tmp_path) is None


def test_empty_file_returns_empty(tmp_path):
    (tmp_path / "MEMORY.md").write_text("  \n", encoding="utf-8")
    assert load_long_term_memory(tmp_path) == ""


def test_load_and_system_message(tmp_path):
    (tmp_path / "MEMORY.md").write_text("回答一律用中文。", encoding="utf-8")
    assert load_long_term_memory(tmp_path) == "回答一律用中文。"
    msg = memory_system_message(tmp_path)
    assert msg["role"] == "system" and "回答一律用中文。" in msg["content"]


def test_truncation(tmp_path):
    (tmp_path / "MEMORY.md").write_text("x" * (MEMORY_MAX_CHARS + 500), encoding="utf-8")
    assert len(load_long_term_memory(tmp_path)) == MEMORY_MAX_CHARS


def test_write_file_guard_for_memory(tmp_path, monkeypatch):
    monkeypatch.setattr("backend.tools.WORKSPACE", tmp_path)
    out = run_tool("write_file", {"path": "MEMORY.md", "content": "偷写记忆"})
    assert "手动编辑" in out
    assert not (tmp_path / "MEMORY.md").exists()


def test_build_messages_order(tmp_path, monkeypatch):
    """上下文顺序必须是 [system, memory, *history, user]。"""
    from backend.main import ChatIn, _build_messages

    monkeypatch.setattr("backend.memory.WORKSPACE", tmp_path)
    (tmp_path / "MEMORY.md").write_text("偏好：回复简洁。", encoding="utf-8")
    body = ChatIn(message="你好", history=[
        {"role": "user", "content": "旧"},
        {"role": "assistant", "content": "回"},
    ])
    msgs = _build_messages(body, "unused-session-id")
    assert [m["role"] for m in msgs] == ["system", "system", "user", "assistant", "user"]
    assert "偏好：回复简洁。" in msgs[1]["content"]
    assert msgs[-1]["content"] == "你好"
