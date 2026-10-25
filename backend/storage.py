"""会话持久化：SQLite 存 user/assistant 轮次（v0.3）。

设计口径（见设计草稿）：
- 只存轮次，不存工具内部消息：与前端 history 的口径一致，下一轮
  组装 messages 时不需要旧 tool_calls，也就没有 tool_call_id 对齐问题。
- session_id 由服务端生成（UUID），前端存 localStorage。
- 加载时只取最近 N 轮（默认 20），其余仍留在库里，改 N 只改 LIMIT。
- 写入时机：user 消息进循环前落库；assistant 最终回复在 done 时落库；
  中途失败不写 assistant，避免半截回复污染下一轮。

标准库 sqlite3，无新增依赖。每次操作开一个新连接（单人使用够用，
也避开 async 事件循环里共享连接的问题），WAL 模式。
"""
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_HISTORY_ROUNDS = int(os.environ.get("HISTORY_ROUNDS", "20"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
  id TEXT PRIMARY KEY,
  title TEXT NOT NULL DEFAULT '新会话',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id TEXT NOT NULL REFERENCES sessions(id),
  role TEXT NOT NULL,
  content TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, id);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def db_path() -> Path:
    data_dir = Path(os.environ.get("DATA_DIR", "./data"))
    return data_dir / "agent.db"


def _connect(path: Path | None = None) -> sqlite3.Connection:
    p = path or db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(p)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(path: Path | None = None) -> Path:
    """建表（不存在才建），返回数据库文件路径。启动时调用一次即可。"""
    p = path or db_path()
    with _connect(p) as conn:
        conn.executescript(SCHEMA)
    return p


def create_session(title: str = "新会话") -> str:
    session_id = str(uuid.uuid4())
    with _connect() as conn:
        conn.execute(
            "INSERT INTO sessions (id, title, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (session_id, title, _now(), _now()),
        )
    return session_id


def ensure_session(session_id: str | None, title_hint: str | None = None) -> str:
    """有 id 且库里存在就复用；有 id 但不存在就按这个 id 建一个；
    没 id 就新建一个。返回最终使用的 session_id。"""
    init_db()
    if session_id:
        with _connect() as conn:
            row = conn.execute(
                "SELECT id FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
        if row:
            return session_id
        title = (title_hint or "").strip()[:20] or "新会话"
        with _connect() as conn:
            conn.execute(
                "INSERT INTO sessions (id, title, created_at, updated_at) VALUES (?, ?, ?, ?)",
                (session_id, title, _now(), _now()),
            )
        return session_id
    title = (title_hint or "").strip()[:20] or "新会话"
    return create_session(title)


def save_message(session_id: str, role: str, content: str) -> None:
    """存一轮消息；首条 user 消息顺手把会话标题写成它的前 20 字。"""
    init_db()
    with _connect() as conn:
        count = conn.execute(
            "SELECT COUNT(*) AS n FROM messages WHERE session_id = ?", (session_id,)
        ).fetchone()["n"]
        conn.execute(
            "INSERT INTO messages (session_id, role, content, created_at) VALUES (?, ?, ?, ?)",
            (session_id, role, content, _now()),
        )
        if role == "user" and count == 0:
            title = content.strip()[:20] or "新会话"
            conn.execute(
                "UPDATE sessions SET title = ?, updated_at = ? WHERE id = ?",
                (title, _now(), session_id),
            )
        else:
            conn.execute(
                "UPDATE sessions SET updated_at = ? WHERE id = ?", (_now(), session_id)
            )


def load_history(session_id: str, rounds: int = DEFAULT_HISTORY_ROUNDS) -> list[dict]:
    """取最近 N 轮，按时间正序返回 [{"role", "content"}]。会话不存在返回 []。"""
    init_db()
    with _connect() as conn:
        rows = conn.execute(
            "SELECT role, content FROM messages WHERE session_id = ?"
            " ORDER BY id DESC LIMIT ?",
            (session_id, rounds * 2),
        ).fetchall()
    return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]


def list_sessions(limit: int = 50) -> list[dict]:
    init_db()
    with _connect() as conn:
        rows = conn.execute(
            "SELECT id, title, created_at, updated_at FROM sessions"
            " ORDER BY updated_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]
