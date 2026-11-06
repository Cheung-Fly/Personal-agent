"""长期记忆：v0.4 整文件注入 → v0.14 条目化 + 按需检索。

v0.4–v0.13 的老办法（历史口径，见 git）：
- 长期记忆 = agent_workspace/MEMORY.md 整文件作为第二条 system 消息
  注入（[system, memory, *history, user]），上限 2000 字符、超了截断。
- 写入：Agent 提议 write_file 写 MEMORY.md，永远走中风险审批卡，
  用户点通过才落盘（v0.5 开的口子）；v0.12 又在流式循环加了护栏，
  治“读完文件却以纯文本收尾、不发 write_file”的概率性失败。

v0.14 改的是“存什么、取什么”，写通道与护栏一律不动（设计草稿钉死）：
- 存：条目化。MEMORY.md 仍是写入落点与人可读导出，但事实的权威
  存储是 SQLite 条目表 memory_items（与会话同库 data/agent.db）：
  一条一事实，带 created_at 与 source；被新内容取代的旧条目标记
  superseded 留档不删，审计可查。文件每次变化（审批写入 / 手动
  编辑 / 迁移）后按内容哈希增量同步成条目：新行进表、消失的
  active 条目标 superseded、没变的不动（保住原始时间与来源）。
- 取：按需检索。组装上下文时用当前问题对 active 条目做关键词
  检索（中文双字切分 + 英文/数字词元，与 v0.7 RAG 同一套“切分 +
  阈值 + 没命中不硬塞”的思路，但零 API、零新依赖：草稿定的 A
  方案，关键词不用 embedding），按覆盖率排序取 top-k，总注入
  ≤ MEMORY_INJECT_MAX_CHARS（默认 1000，比老办法的 2000 更紧）；
  没命中就整条 system 消息不注入——无关问题不再被旧偏好污染，
  也不白占预算。
- 检索漏命中是这版的核心失败形态（表现像“记忆丢了”，与老办法
  的“截断丢了”不同）：阈值 MEMORY_MIN_SCORE 是覆盖率口径的经验
  值，验收用写入-召回链路 + 反向用例量化，漏了调阈值/k，不放宽
  判定（见 v0.14 设计草稿“边界与风险”）。

来源（source）取值：migrated（旧文件迁移）/ conversation（对话里
经审批写入后同步）/ file_edit（手动改文件后同步）。
"""
import hashlib
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .storage import db_path
from .tools import WORKSPACE

MEMORY_FILE_NAME = "MEMORY.md"
# 老口径保留：load_long_term_memory 的整文件读取上限（迁移/导出用）。
MEMORY_MAX_CHARS = int(os.environ.get("MEMORY_MAX_CHARS", "2000"))
# v0.14 检索注入：top-k 条数、总字符预算、最低覆盖率（命中判定）。
MEMORY_TOP_K = int(os.environ.get("MEMORY_TOP_K", "5"))
MEMORY_INJECT_MAX_CHARS = int(os.environ.get("MEMORY_INJECT_MAX_CHARS", "1000"))
MEMORY_MIN_SCORE = float(os.environ.get("MEMORY_MIN_SCORE", "0.2"))

MEMORY_HEADER = "以下是从用户的长期记忆库中按与当前问题相关度检索到的条目（长期记忆库由 agent_workspace/MEMORY.md 同步而来，想改写时可提议 write_file 改写 MEMORY.md，须经用户在审批卡上确认后才生效），请在回复中遵守："

_ITEMS_SCHEMA = """
CREATE TABLE IF NOT EXISTS memory_items (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  text TEXT NOT NULL,
  source TEXT NOT NULL,
  created_at TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'active',
  superseded_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_memory_items_status ON memory_items(status);
CREATE TABLE IF NOT EXISTS memory_sync (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  file_hash TEXT NOT NULL,
  synced_at TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def memory_path(workspace: Path | None = None) -> Path:
    return (workspace or WORKSPACE) / MEMORY_FILE_NAME


def load_long_term_memory(workspace: Path | None = None) -> str:
    """读长期记忆文件全文；缺失/为空返回 ""；超长截断到 MEMORY_MAX_CHARS。

    v0.14 起它只是“文件内容”的读取口（同步与迁移用），不再是注入源。"""
    p = memory_path(workspace)
    try:
        text = p.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return ""
    if not text:
        return ""
    if len(text) > MEMORY_MAX_CHARS:
        return text[:MEMORY_MAX_CHARS]
    return text


def _read_file_raw(workspace: Path | None = None) -> str:
    """同步用全文读取：不截断（截断会把后半文件误判成“事实消失”
    而错误 supersede）。读不到按空文件处理。"""
    try:
        return memory_path(workspace).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


# ---------- 条目表（SQLite，与会话同库） ----------

def _connect(db: Path | None = None) -> sqlite3.Connection:
    p = db or db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(p)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(_ITEMS_SCHEMA)
    return conn


def list_memory_items(db: Path | None = None, *, include_superseded: bool = False) -> list[dict]:
    """列条目（默认只 active），按 id 正序。"""
    with _connect(db) as conn:
        if include_superseded:
            rows = conn.execute("SELECT * FROM memory_items ORDER BY id").fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM memory_items WHERE status = 'active' ORDER BY id"
            ).fetchall()
    return [dict(r) for r in rows]


def _normalize(text: str) -> str:
    """条目同一性判定：去首尾空白、内部连续空白折叠。标点差异算不同
    条目（宁可多留一行 superseded，也不误杀一条事实）。"""
    return re.sub(r"\s+", " ", (text or "").strip())


# ---------- 文件 → 条目：拆分与分类 ----------

_SENTENCE_ENDERS = "。！？；"
_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_BULLET_RE = re.compile(r"^\s*(?:[-*+]|\d+[.、)])\s+(.*)$")


def split_memory_entries(text: str) -> list[dict]:
    """把 MEMORY.md 文本拆成候选条目并分类（迁移清单与同步共用）。

    返回 [{raw, text, kind}]，kind 三类：
    - item：单行单事实，可直接入条目表（项目符号行或独立成行的句子）；
    - pending：一行里塞了多个事实（句末标点 ≥2）或过长（>200 字符）
      ——拆分有歧义，迁移时只列清单不迁入，等人工确认；
    - skip：标题、空行、HTML 注释等结构行，不是事实，丢弃留痕。
    """
    text = _COMMENT_RE.sub("", text or "")
    out = []
    for line in text.splitlines():
        raw = line.rstrip()
        stripped = raw.strip()
        if not stripped:
            out.append({"raw": raw, "text": "", "kind": "skip"})
            continue
        if stripped.startswith("#"):
            out.append({"raw": raw, "text": "", "kind": "skip"})
            continue
        m = _BULLET_RE.match(raw)
        content = (m.group(1) if m else stripped).strip()
        enders = sum(content.count(c) for c in _SENTENCE_ENDERS)
        # 一行多事实的启发式：句末标点 ≥2，或全角逗号 ≥2（“在成都，
        # 喝美式，周末爬山”式并列）；单个逗号是限定语，不算多事实。
        if enders >= 2 or content.count("，") >= 2 or len(content) > 200:
            out.append({"raw": raw, "text": content, "kind": "pending"})
        else:
            out.append({"raw": raw, "text": content, "kind": "item"})
    return out


def _split_sentences(content: str) -> list[str]:
    """把一行多事实按句末标点切开（运行时同步用：内容已经过审批
    或是用户手写，切错的代价小于整行当一条的检索噪声）。"""
    parts = re.split(r"(?<=[。！？；])", content)
    return [p.strip() for p in parts if p.strip()]


def plan_migration(text: str) -> dict:
    """迁移清单（不落库）：{migrate: [...], pending: [...], skipped: [...]}，
    每项是原文行。供人工抽查（迁移脚本与设计草稿的验收口径）。"""
    plan = {"migrate": [], "pending": [], "skipped": []}
    for e in split_memory_entries(text):
        if e["kind"] == "item":
            plan["migrate"].append(e["text"])
        elif e["kind"] == "pending":
            plan["pending"].append(e["raw"])
        else:
            plan["skipped"].append(e["raw"])
    return plan


# ---------- 同步：文件是写入落点，条目表是权威存储 ----------

def sync_memory_from_file(workspace: Path | None = None, db: Path | None = None,
                          *, source: str = "file_edit",
                          split_pending: bool = True,
                          only_unambiguous: bool = False) -> dict:
    """把当前 MEMORY.md 内容增量同步进条目表，返回统计
    {added, superseded, kept, skipped, pending}。

    - 新出现的事实 → 插入（source 标来源）；消失的 active 条目 →
      superseded（留档不删）；仍在的条目原样保留（时间/来源不漂移）。
    - split_pending=True（运行时）：一行多事实按句切开分别成条；
      False 时整行作一条。only_unambiguous=True（评审式迁移 apply
      专用）：pending 行不迁入、也不参与 diff（不因它没进表就误伤
      别的条目），但同步哈希照常记下——文件再变时走运行时全量同步。
    - 同步哈希落在 memory_sync 表：ensure_memory_synced 靠它判断
      文件是否被动过，没动就不重复 diff。
    """
    raw = _read_file_raw(workspace)
    file_hash = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    entries = split_memory_entries(raw)
    new_texts: list[str] = []
    stats = {"added": 0, "superseded": 0, "kept": 0, "skipped": 0, "pending": 0}
    for e in entries:
        if e["kind"] == "skip":
            stats["skipped"] += 1
            continue
        if e["kind"] == "pending":
            stats["pending"] += 1
            if only_unambiguous:
                continue
            if split_pending:
                new_texts.extend(_split_sentences(e["text"]))
            else:
                new_texts.append(e["text"])
            continue
        new_texts.append(e["text"])
    new_norm = {_normalize(t): t for t in new_texts if _normalize(t)}
    with _connect(db) as conn:
        active = conn.execute(
            "SELECT id, text FROM memory_items WHERE status = 'active'"
        ).fetchall()
        active_norm = {_normalize(r["text"]): r for r in active}
        for norm, row in active_norm.items():
            if norm not in new_norm:
                conn.execute(
                    "UPDATE memory_items SET status = 'superseded', superseded_at = ?"
                    " WHERE id = ?",
                    (_now(), row["id"]),
                )
                stats["superseded"] += 1
            else:
                stats["kept"] += 1
        for norm, text in new_norm.items():
            if norm not in active_norm:
                conn.execute(
                    "INSERT INTO memory_items (text, source, created_at, status)"
                    " VALUES (?, ?, ?, 'active')",
                    (text, source, _now()),
                )
                stats["added"] += 1
        conn.execute(
            "INSERT INTO memory_sync (id, file_hash, synced_at) VALUES (1, ?, ?)"
            " ON CONFLICT(id) DO UPDATE SET file_hash = excluded.file_hash,"
            " synced_at = excluded.synced_at",
            (file_hash, _now()),
        )
    stats["file_hash"] = file_hash
    return stats


def ensure_memory_synced(workspace: Path | None = None, db: Path | None = None) -> None:
    """注入前调用：文件哈希与上次同步不一致才增量同步。

    首次同步（表里还没有同步记录）标 source=migrated——存量文件
    本来就是“从老办法迁过来的”；之后的变化按 file_edit。注意：
    经审批的 write_file 写入在 tools.run_tool 里已即时同步
    （source=conversation），这里的哈希比对会发现已同步、不重复做。
    同步失败不能拖垮对话：吞掉异常只打日志，注入退化为无记忆。"""
    try:
        raw = _read_file_raw(workspace)
        file_hash = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        with _connect(db) as conn:
            row = conn.execute(
                "SELECT file_hash FROM memory_sync WHERE id = 1"
            ).fetchone()
        if row and row["file_hash"] == file_hash:
            return
        source = "migrated" if row is None else "file_edit"
        stats = sync_memory_from_file(workspace, db, source=source)
        print(f"[memory] 条目同步（{source}）：新增 {stats['added']}、"
              f"取代 {stats['superseded']}、保留 {stats['kept']}")
    except Exception as e:  # 同步是加法，坏了不许打断 Agent 循环
        print(f"[memory] 条目同步失败（本轮按无记忆处理）: {type(e).__name__}: {e}")


# ---------- 检索：关键词覆盖率（A 方案，零 API） ----------

def _terms(text: str) -> set[str]:
    """词元集：英文/数字按整词（小写），中文按双字切分（单字词元
    只在整段只有一个汉字时保留）。与 v0.7 RAG 的中文双字切分同一
    思路——记忆条目短，词元重叠比 TF-IDF 权重更稳、也可复现。"""
    terms: set[str] = set()
    for w in re.findall(r"[a-zA-Z0-9]+", (text or "").lower()):
        terms.add(w)
    for run in re.findall(r"[一-鿿]+", text or ""):
        if len(run) == 1:
            terms.add(run)
        else:
            terms.update(run[i:i + 2] for i in range(len(run) - 1))
    return terms


def score_item(query_terms: set[str], item_text: str) -> tuple[float, int]:
    """返回 (覆盖率, 命中词元数)：覆盖率 = 命中的查询词元 / 查询词元总数。
    查询为空返回 (0, 0)。"""
    if not query_terms:
        return 0.0, 0
    matched = len(query_terms & _terms(item_text))
    return matched / len(query_terms), matched


def _is_hit(score: float, matched: int, query_terms: set[str]) -> bool:
    """命中判定：覆盖率过阈值且至少命中 2 个词元；或查询本身很短
    （≤2 词元）且全部命中——防“幸运数字？”这类短问句被词元数
    门槛误杀。两个条件都不满足就是没命中，不硬塞（草稿口径）。"""
    if matched >= 2 and score >= MEMORY_MIN_SCORE:
        return True
    return 0 < len(query_terms) <= 2 and matched == len(query_terms)


def retrieve_memory(query: str, db: Path | None = None, *,
                    top_k: int | None = None,
                    max_chars: int | None = None) -> list[dict]:
    """按当前问题检索 active 条目：命中才返回，按 (覆盖率, 命中数,
    新近) 排序取 top-k，总字符 ≤ max_chars（第一条命中即使超预算
    也截断收下，保证最相关的一条不丢）。没命中返回 []。"""
    top_k = MEMORY_TOP_K if top_k is None else top_k
    max_chars = MEMORY_INJECT_MAX_CHARS if max_chars is None else max_chars
    q = _terms(query or "")
    if not q:
        return []
    scored = []
    for item in list_memory_items(db):
        score, matched = score_item(q, item["text"])
        if _is_hit(score, matched, q):
            scored.append((score, matched, item["id"], item))
    scored.sort(key=lambda t: (t[0], t[1], t[2]), reverse=True)
    out, total = [], 0
    for _score, _matched, _id, item in scored[:top_k]:
        line = f"- {item['text']}"
        if total + len(line) > max_chars:
            if not out:
                out.append({**item, "_line": line[:max_chars]})
            break
        out.append({**item, "_line": line})
        total += len(line)
    return out


def memory_system_message(query: str = "", workspace: Path | None = None,
                          db: Path | None = None) -> dict | None:
    """v0.14：先确保条目与文件同步，再按 query 检索注入。

    命中才返回第二条 system 消息；没命中 / 无条目返回 None（上下文
    形态退回 [system, *history, user]，与无记忆时一致）。"""
    ensure_memory_synced(workspace, db)
    hits = retrieve_memory(query, db)
    if not hits:
        print(f"[memory] 本轮检索无命中，不注入（query 前 30 字：{(query or '')[:30]!r}）")
        return None
    print(f"[memory] 本轮检索注入 {len(hits)} 条长期记忆")
    body = "\n".join(h["_line"] for h in hits)
    return {"role": "system", "content": f"{MEMORY_HEADER}\n\n{body}"}
