"""小 RAG：项目文档语义检索（v0.7）。

设计口径（见设计草稿 v0.7-小RAG设计草稿.md + 2026-10-29 定案）：
- 给 Agent 加一个 search_docs 工具：先检索出最相关的几段再喂模型，
  而不是靠模型记忆猜、或 read_file 全文读撑爆 v0.6 的上下文预算。
- 检索算法：Chung 2026-10-29 拍板用智谱 embedding API（不用草稿
  推荐的本地 TF-IDF）——语义相近、换个说法也能命中。模型
  embedding-3、维度 1024，与聊天共用 LLM_BASE_URL / LLM_API_KEY。
- 语料：默认 agent_workspace/docs/ 下的 .md，只读；MEMORY.md 排除
  （已由 v0.4 每轮注入，进检索是重复喂）。工具定低风险自动执行。
- 切块：先按 Markdown 标题分段，超长段按 800 字符切、100 字符重叠，
  避免一句话被拦腰切断后两块都匹配不上。
- 向量缓存：JSON 落盘 data/rag_index.json，按内容哈希复用——文件
  改了只重嵌变化的块；模型/维度配置变了整表重建。
- 诚实未命中：最高分低于阈值（默认余弦 0.4，v0.8 按真机校准从
  0.3 回填，见 RAG_MIN_SCORE 处注释）就明确返回“未找到相关文档”，
  不硬凑不相关段落喂模型——防幻觉的一半在检索侧。
- 失败不打断循环：无 Key、接口报错、语料为空都返回清楚的错误
  文本（复用 v0.1 的失败文本回喂口径），模型可改用 read_file 或
  如实回答；本模块对外不抛异常。

只用标准库 + 项目已有的 httpx（embedding 调用照 main.py 调 LLM 的
写法），零新增依赖。
"""
import hashlib
import json
import math
import os
import re
from pathlib import Path

import httpx

from .tools import WORKSPACE

# 与聊天共用同一 base URL 与 Key（.env 里同一套配置）。
LLM_API_KEY = os.environ.get("LLM_API_KEY", "")
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "https://api.openai.com/v1").rstrip("/")
EMBEDDING_MODEL = os.environ.get("EMBEDDING_MODEL", "embedding-3")
EMBEDDING_DIMENSIONS = int(os.environ.get("EMBEDDING_DIMENSIONS", "1024"))
RAG_DOCS_DIR = os.environ.get("RAG_DOCS_DIR", "")  # 空 = WORKSPACE/docs
RAG_TOP_K = int(os.environ.get("RAG_TOP_K", "3"))
# 阈值默认 0.4（v0.8 回填）：2026-10-29 真机校准——无关问题对真实
# 向量的余弦分约 0.34–0.35、相关问题 0.52/0.60，原默认 0.3 会把无关
# 段落当命中喂给模型，服务器当时已靠 .env 调到 0.4，这里把仓库默认
# 值对齐。注意这只是 3 个样本点夹出的经验值、不是统计结论；评测集
# 任务 4/5（eval/）就是它的回归用例，攒够数据再校准。
RAG_MIN_SCORE = float(os.environ.get("RAG_MIN_SCORE", "0.4"))

CHUNK_CHARS = 800  # 单块字符上限（同草稿）
CHUNK_OVERLAP = 100  # 超长切分时相邻块的重叠字符数
RESULT_MAX_CHARS = 2000  # 一次检索返回给模型的文本总上限（守 v0.6 预算）
EMBED_BATCH_SIZE = 64  # embedding 接口单次最多 64 条

_HEADING_RE = re.compile(r"^#{1,6}\s+")


class RagError(Exception):
    """检索链路内部错误：统一在 search_docs 里转成失败文本回喂。"""


def docs_dir(workspace: Path | None = None) -> Path:
    """语料目录：RAG_DOCS_DIR 设了就用它，否则 WORKSPACE/docs
    （路径解析与 memory.py 的 WORKSPACE 口径一致）。"""
    if RAG_DOCS_DIR:
        return Path(RAG_DOCS_DIR)
    return (workspace or WORKSPACE) / "docs"


def index_path() -> Path:
    """向量索引缓存位置：data/rag_index.json（与审计日志、会话库
    同放 DATA_DIR；data/ 已进 .gitignore，不进 git）。"""
    return Path(os.environ.get("DATA_DIR", "./data")) / "rag_index.json"


# ---------- 切块 ----------

def _split_long(text: str) -> list[str]:
    """超长段按 CHUNK_CHARS 切，相邻块重叠 CHUNK_OVERLAP 字符。"""
    if len(text) <= CHUNK_CHARS:
        return [text]
    parts = []
    step = CHUNK_CHARS - CHUNK_OVERLAP
    for start in range(0, len(text), step):
        parts.append(text[start:start + CHUNK_CHARS])
        if start + CHUNK_CHARS >= len(text):
            break
    return parts


def chunk_markdown(text: str, source: str) -> list[dict]:
    """把一篇 Markdown 切成块：先按标题分段，段内超长再切。

    每块 {source, heading, text}：text 带上标题行一起进 embedding
    （标题是最强的语义信号），heading 单独记下来供返回时标注出处。"""
    sections: list[tuple[str, list[str]]] = []
    heading = ""
    lines: list[str] = []
    for line in text.splitlines():
        if _HEADING_RE.match(line):
            if lines:
                sections.append((heading, lines))
            heading = line.lstrip("#").strip()
            lines = [line]
        else:
            lines.append(line)
    if lines:
        sections.append((heading, lines))
    chunks = []
    for sec_heading, sec_lines in sections:
        body = "\n".join(sec_lines).strip()
        if not body:
            continue
        for part in _split_long(body):
            chunks.append({"source": source, "heading": sec_heading, "text": part})
    return chunks


def _content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def collect_chunks(docs: Path, workspace: Path | None = None) -> list[dict]:
    """扫语料目录，返回全部块（带来源文件、mtime、内容哈希）。
    只收 .md，排除 MEMORY.md（v0.4 已注入，不重复进检索）。"""
    chunks = []
    for p in sorted(docs.rglob("*.md")):
        if p.name == "MEMORY.md":
            continue
        try:
            text = p.read_text(encoding="utf-8")
            mtime = p.stat().st_mtime
        except (OSError, UnicodeDecodeError):
            continue  # 读不了的文件跳过，不让一篇坏文档拖垮整个检索
        try:
            source = str(p.relative_to(workspace or WORKSPACE))
        except ValueError:
            source = p.name
        for c in chunk_markdown(text, source):
            c["file_mtime"] = mtime
            c["content_hash"] = _content_hash(c["text"])
            chunks.append(c)
    return chunks


# ---------- embedding ----------

def embed_texts(texts: list[str]) -> list[list[float]]:
    """调 {LLM_BASE_URL}/embeddings 批量算向量，≤64 条/次。
    失败抛 RagError（无 Key、接口报错、返回条数对不上）。"""
    if not LLM_API_KEY:
        raise RagError("未配置 LLM_API_KEY，无法调用 embedding 接口")
    vectors: list[list[float]] = []
    with httpx.Client(timeout=60) as client:
        for i in range(0, len(texts), EMBED_BATCH_SIZE):
            batch = texts[i:i + EMBED_BATCH_SIZE]
            try:
                r = client.post(
                    f"{LLM_BASE_URL}/embeddings",
                    headers={"Authorization": f"Bearer {LLM_API_KEY}"},
                    json={"model": EMBEDDING_MODEL, "input": batch,
                          "dimensions": EMBEDDING_DIMENSIONS},
                )
                r.raise_for_status()
                data = r.json()
            except Exception as e:
                raise RagError(f"embedding 接口调用失败: {type(e).__name__}: {e}") from e
            usage = data.get("usage")
            if usage:
                print(f"[rag] embeddings usage: {usage}")
            items = sorted(data.get("data") or [], key=lambda d: d.get("index", 0))
            if len(items) != len(batch):
                raise RagError(f"embedding 返回条数对不上：要 {len(batch)} 条、回 {len(items)} 条")
            vectors.extend(item["embedding"] for item in items)
    return vectors


# ---------- 索引缓存 ----------

def _load_index(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def ensure_index(chunks: list[dict], embed_fn=None, index_file: Path | None = None) -> list[dict]:
    """保证每块都有向量，返回带 embedding 的块列表。

    缓存按内容哈希复用：没变的块直接用旧向量，只给新块/改过的块
    调接口——改一篇文档不会把整库重嵌一遍。缓存里的模型与当前
    EMBEDDING_MODEL 不符、或维度与 EMBEDDING_DIMENSIONS 不符
    （注入 fake embed 的测试除外），整表作废重建：换了模型，
    旧向量和新向量不在一个空间里，混着比余弦没意义。"""
    ef = embed_fn or embed_texts
    path = index_file or index_path()
    cached = _load_index(path)
    reuse: dict[str, dict] = {}
    if cached and cached.get("model") == EMBEDDING_MODEL:
        if embed_fn is not None or cached.get("dimensions") == EMBEDDING_DIMENSIONS:
            for c in cached.get("chunks") or []:
                reuse[c["content_hash"]] = c
    missing = [c for c in chunks if c["content_hash"] not in reuse]
    fresh: dict[str, list[float]] = {}
    if missing:
        vecs = ef([c["text"] for c in missing])
        if len(vecs) != len(missing):
            raise RagError(f"embedding 返回条数对不上：要 {len(missing)} 条、回 {len(vecs)} 条")
        if embed_fn is None and vecs and len(vecs[0]) != EMBEDDING_DIMENSIONS:
            raise RagError(f"embedding 维度是 {len(vecs[0])}，与配置 EMBEDDING_DIMENSIONS={EMBEDDING_DIMENSIONS} 不一致")
        fresh = {c["content_hash"]: v for c, v in zip(missing, vecs)}
    out = []
    for c in chunks:
        entry = dict(c)
        entry["embedding"] = fresh.get(c["content_hash"]) or reuse[c["content_hash"]]["embedding"]
        out.append(entry)
    if out:
        payload = {
            "model": EMBEDDING_MODEL,
            "dimensions": len(out[0]["embedding"]),
            "chunks": out,
        }
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        except OSError as e:
            print(f"[rag] 写索引缓存失败（不影响本次检索）: {e}")
    return out


# ---------- 检索 ----------

def cosine(a: list[float], b: list[float]) -> float:
    """余弦相似度：只看方向不看长度——文档长短不影响相关性判断。
    维度对不上或零向量返回 0（不可比就当不相关）。"""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def search_docs(query: str, *, top_k: int | None = None, min_score: float | None = None,
                embed_fn=None, docs: Path | None = None,
                index_file: Path | None = None,
                workspace: Path | None = None) -> str:
    """search_docs 工具的实际入口：返回给模型的文本，永不抛异常。

    命中：带出处的段落，总长约 2000 字符封顶；未命中/出错：明确
    的说明文本，模型据此改用 read_file 或如实回答。"""
    top_k = RAG_TOP_K if top_k is None else top_k
    min_score = RAG_MIN_SCORE if min_score is None else min_score
    query = (query or "").strip()
    if not query:
        return "工具执行失败: search_docs 需要非空的 query"
    try:
        d = docs or docs_dir(workspace)
        if not d.is_dir():
            return f"未找到相关文档：语料目录 {d} 不存在，还没有可检索的文档；可改用 list_files / read_file 直接看工作区文件"
        chunks = collect_chunks(d, workspace)
        if not chunks:
            return "未找到相关文档：语料目录里没有可检索的 .md 文档（MEMORY.md 不进检索）"
        indexed = ensure_index(chunks, embed_fn=embed_fn, index_file=index_file)
        ef = embed_fn or embed_texts
        qvec = ef([query])[0]
        scored = sorted(
            ((cosine(qvec, c["embedding"]), c) for c in indexed),
            key=lambda t: t[0], reverse=True,
        )
        hits = [(s, c) for s, c in scored[:top_k] if s >= min_score]
        if not hits:
            return (f"未找到相关文档：没有与「{query}」足够相关的内容"
                    f"（最高分 {scored[0][0]:.2f}，低于阈值 {min_score}）；"
                    "请如实说明文档里没写，或换个问法再检索")
        parts = [f"在项目文档里检索「{query}」，找到 {len(hits)} 段相关内容："]
        total = len(parts[0])
        for score, c in hits:
            loc = c["source"] + (f"#{c['heading']}" if c["heading"] else "")
            block = f"\n[来源：{loc}]（相关度 {score:.2f}）\n{c['text']}"
            if total + len(block) > RESULT_MAX_CHARS:
                block = block[:RESULT_MAX_CHARS - total]
                if block:
                    parts.append(block)
                break
            parts.append(block)
            total += len(block)
        return "\n".join(parts)
    except RagError as e:
        return (f"工具执行失败: 文档检索不可用（{e}）；"
                "可改用 read_file 直接读文档，或如实说明暂时检索不了")
    except Exception as e:  # 检索失败也不能打断 Agent 循环
        return f"工具执行失败: search_docs 出错（{type(e).__name__}: {e}）"
