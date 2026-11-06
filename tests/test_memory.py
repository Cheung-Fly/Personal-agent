"""长期记忆测试：v0.4 的文件读取口径 + v0.14 的检索注入口径。

v0.14 起 memory_system_message 不再整文件注入：先把文件同步成
条目，再按 query 检索命中才返回第二条 system 消息。整文件读取
（load_long_term_memory）只剩同步/迁移在用，截断口径保留。
python -m pytest tests/ -q"""
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


def _db(tmp_path):
    return tmp_path / "data" / "agent.db"


def test_missing_file_returns_empty(tmp_path):
    assert load_long_term_memory(tmp_path) == ""
    assert memory_system_message("任意问题", tmp_path, _db(tmp_path)) is None


def test_empty_file_returns_empty(tmp_path):
    (tmp_path / "MEMORY.md").write_text("  \n", encoding="utf-8")
    assert load_long_term_memory(tmp_path) == ""


def test_load_full_text(tmp_path):
    (tmp_path / "MEMORY.md").write_text("回答一律用中文。", encoding="utf-8")
    assert load_long_term_memory(tmp_path) == "回答一律用中文。"


def test_system_message_only_on_retrieval_hit(tmp_path):
    """v0.14：相关问题才注入命中的条目；无关问题不注入（返回 None）。"""
    (tmp_path / "MEMORY.md").write_text(
        "- 偏好：回复默认用简体中文。\n- 项目用 FastAPI 加智谱 GLM。\n",
        encoding="utf-8")
    msg = memory_system_message("请用简体中文回复我", tmp_path, _db(tmp_path))
    assert msg["role"] == "system" and "简体中文" in msg["content"]
    assert memory_system_message("今天天气怎么样", tmp_path, _db(tmp_path)) is None


def test_truncation(tmp_path):
    (tmp_path / "MEMORY.md").write_text("x" * (MEMORY_MAX_CHARS + 500), encoding="utf-8")
    assert len(load_long_term_memory(tmp_path)) == MEMORY_MAX_CHARS


def test_write_file_guard_for_memory(tmp_path, monkeypatch):
    monkeypatch.setattr("backend.tools.WORKSPACE", tmp_path)
    out = run_tool("write_file", {"path": "MEMORY.md", "content": "偷写记忆"})
    assert "手动编辑" in out
    assert not (tmp_path / "MEMORY.md").exists()


def test_build_messages_order(tmp_path, monkeypatch):
    """上下文顺序必须是 [system, memory, *history, user]（记忆命中时）。"""
    from backend.main import ChatIn, _build_messages

    monkeypatch.setattr("backend.memory.WORKSPACE", tmp_path)
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    (tmp_path / "MEMORY.md").write_text("- 偏好：回复默认用简体中文。", encoding="utf-8")
    body = ChatIn(message="请用简体中文回复我", history=[
        {"role": "user", "content": "旧"},
        {"role": "assistant", "content": "回"},
    ])
    msgs = _build_messages(body, "unused-session-id")
    assert [m["role"] for m in msgs] == ["system", "system", "user", "assistant", "user"]
    assert "简体中文" in msgs[1]["content"]
    assert msgs[-1]["content"] == "请用简体中文回复我"
