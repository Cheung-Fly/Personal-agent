"""v0.3 会话持久化的单测：只测 SQLite 存取，不依赖 LLM。

跑法： python -m pytest tests/ -q
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _storage(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    import backend.storage as storage
    return storage


def test_round_trip(tmp_path, monkeypatch):
    s = _storage(tmp_path, monkeypatch)
    sid = s.ensure_session(None, title_hint="你好")
    s.save_message(sid, "user", "你好")
    s.save_message(sid, "assistant", "你好，我在")
    s.save_message(sid, "user", "写个 hello.py")
    s.save_message(sid, "assistant", "写好了")
    assert s.load_history(sid) == [
        {"role": "user", "content": "你好"},
        {"role": "assistant", "content": "你好，我在"},
        {"role": "user", "content": "写个 hello.py"},
        {"role": "assistant", "content": "写好了"},
    ]


def test_load_only_recent_rounds(tmp_path, monkeypatch):
    s = _storage(tmp_path, monkeypatch)
    sid = s.ensure_session(None)
    for i in range(25):
        s.save_message(sid, "user", f"第{i}问")
        s.save_message(sid, "assistant", f"第{i}答")
    history = s.load_history(sid, rounds=20)
    assert len(history) == 40  # 20 轮 = 40 条
    assert history[0] == {"role": "user", "content": "第5问"}
    assert history[-1] == {"role": "assistant", "content": "第24答"}


def test_ensure_session_reuse_and_custom_id(tmp_path, monkeypatch):
    s = _storage(tmp_path, monkeypatch)
    sid = s.ensure_session(None)
    assert s.ensure_session(sid) == sid  # 已存在就复用，不新建
    assert s.ensure_session("custom-id-123") == "custom-id-123"
    assert s.load_history("custom-id-123") == []


def test_title_from_first_message(tmp_path, monkeypatch):
    s = _storage(tmp_path, monkeypatch)
    sid = s.ensure_session(None)
    s.save_message(sid, "user", "帮我写一个斐波那契函数并跑一下看看结果")
    sessions = s.list_sessions()
    assert sessions[0]["id"] == sid
    assert sessions[0]["title"] == "帮我写一个斐波那契函数并跑一下看看结果"[:20]


def test_list_sessions_newest_first(tmp_path, monkeypatch):
    s = _storage(tmp_path, monkeypatch)
    first = s.ensure_session(None, title_hint="第一个会话")
    second = s.ensure_session(None, title_hint="第二个会话")
    # 给第一个会话再写一条消息，它的 updated_at 变最新，应排到前面
    s.save_message(first, "user", "又来一轮")
    ordered = [x["id"] for x in s.list_sessions()]
    assert ordered[0] == first
    assert set(ordered) == {first, second}


def test_unknown_session_history_is_empty(tmp_path, monkeypatch):
    s = _storage(tmp_path, monkeypatch)
    assert s.load_history("不存在的会话") == []
