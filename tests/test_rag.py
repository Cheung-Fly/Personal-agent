"""小 RAG（v0.7）测试：用确定性假向量，不依赖真实 embedding 接口。
python -m pytest tests/ -q"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("WORKSPACE_DIR", "./agent_workspace_test")

import backend.rag as rag  # noqa: E402
from backend.approval import LOW, classify  # noqa: E402

# 假向量：固定词表的词袋计数。含同一关键词的文本向量方向一致，
# 余弦就高；完全无关的文本是零向量，余弦 0——确定性、可复现。
VOCAB = ["审批", "分级", "预算", "记忆", "会话", "部署"]


def fake_embed(texts):
    return [[float(t.count(w)) for w in VOCAB] for t in texts]


class CountingEmbed:
    """包一层计数，用来验缓存失效时到底重嵌了几块。"""

    def __init__(self):
        self.batches = []

    def __call__(self, texts):
        self.batches.append(list(texts))
        return fake_embed(texts)

    @property
    def embedded_texts(self):
        return [t for batch in self.batches for t in batch]


def _write_docs(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "approval.md").write_text(
        "# 审批分级\n\n工具调用先审批分级：只读自动、改动确认、高危拒绝。\n",
        encoding="utf-8")
    (docs / "budget.md").write_text(
        "# 上下文预算\n\n预算超了从最旧轮次截断，预算默认 8000。\n",
        encoding="utf-8")
    return docs


# ---------- 切块 ----------

def test_chunk_by_headings():
    text = "# 标题一\n内容一\n## 标题二\n内容二\n"
    chunks = rag.chunk_markdown(text, "docs/x.md")
    assert [c["heading"] for c in chunks] == ["标题一", "标题二"]
    assert all(c["source"] == "docs/x.md" for c in chunks)
    assert "内容一" in chunks[0]["text"] and "内容二" in chunks[1]["text"]


def test_chunk_long_section_split_with_overlap():
    body = "".join(f"第{i}句。" for i in range(200))  # 远超 800 字符
    chunks = rag.chunk_markdown(f"# 长文\n{body}", "docs/long.md")
    assert len(chunks) > 1
    assert all(len(c["text"]) <= rag.CHUNK_CHARS + len("# 长文\n") for c in chunks)
    # 相邻块重叠：后一块开头应包含前一块结尾的一部分（不断句设计）
    first, second = chunks[0]["text"], chunks[1]["text"]
    overlap_probe = first[-50:]
    assert any(overlap_probe[i:i + 20] in second for i in range(0, 30))


def test_collect_excludes_memory_and_non_md(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "MEMORY.md").write_text("# 记忆\n审批 分级", encoding="utf-8")
    (docs / "note.txt").write_text("审批 分级", encoding="utf-8")
    (docs / "a.md").write_text("# A\n正文", encoding="utf-8")
    chunks = rag.collect_chunks(docs, tmp_path)
    assert [c["source"] for c in chunks] == ["docs/a.md"]
    assert chunks[0]["content_hash"] and chunks[0]["file_mtime"] > 0


# ---------- 索引缓存 ----------

def test_index_cache_reuse_and_invalidation(tmp_path):
    docs = _write_docs(tmp_path)
    index_file = tmp_path / "rag_index.json"
    counter = CountingEmbed()
    chunks = rag.collect_chunks(docs, tmp_path)
    indexed = rag.ensure_index(chunks, embed_fn=counter, index_file=index_file)
    assert len(counter.embedded_texts) == len(chunks)  # 首次全嵌
    assert index_file.exists()

    # 没改文件：再建一次，一块都不重嵌
    counter2 = CountingEmbed()
    rag.ensure_index(rag.collect_chunks(docs, tmp_path),
                     embed_fn=counter2, index_file=index_file)
    assert counter2.embedded_texts == []

    # 只改一篇里的一块：只重嵌变化的那块
    (docs / "budget.md").write_text(
        "# 上下文预算\n\n预算改成 6000，部署时注意。\n", encoding="utf-8")
    counter3 = CountingEmbed()
    rag.ensure_index(rag.collect_chunks(docs, tmp_path),
                     embed_fn=counter3, index_file=index_file)
    assert len(counter3.embedded_texts) == 1
    assert "6000" in counter3.embedded_texts[0]
    assert len(indexed) == len(chunks)


def test_index_rebuilt_when_model_changes(tmp_path, monkeypatch):
    docs = _write_docs(tmp_path)
    index_file = tmp_path / "rag_index.json"
    chunks = rag.collect_chunks(docs, tmp_path)
    rag.ensure_index(chunks, embed_fn=fake_embed, index_file=index_file)
    monkeypatch.setattr(rag, "EMBEDDING_MODEL", "embedding-9")
    counter = CountingEmbed()
    rag.ensure_index(chunks, embed_fn=counter, index_file=index_file)
    assert len(counter.embedded_texts) == len(chunks)  # 换模型 = 整表重建


# ---------- 检索 ----------

def test_search_hit_returns_source(tmp_path):
    docs = _write_docs(tmp_path)
    out = rag.search_docs("审批分几级", embed_fn=fake_embed, docs=docs,
                          index_file=tmp_path / "idx.json", workspace=tmp_path)
    assert "approval.md" in out and "审批分级" in out
    assert "相关度" in out


def test_search_miss_is_honest(tmp_path):
    docs = _write_docs(tmp_path)
    out = rag.search_docs("今天天气怎么样", embed_fn=fake_embed, docs=docs,
                          index_file=tmp_path / "idx.json", workspace=tmp_path)
    assert out.startswith("未找到相关文档")


def test_search_empty_or_missing_corpus(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    out = rag.search_docs("审批", embed_fn=fake_embed, docs=empty,
                          index_file=tmp_path / "idx.json", workspace=tmp_path)
    assert "未找到相关文档" in out
    out2 = rag.search_docs("审批", embed_fn=fake_embed, docs=tmp_path / "nope",
                           index_file=tmp_path / "idx.json", workspace=tmp_path)
    assert "未找到相关文档" in out2


def test_search_result_capped(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "big.md").write_text(
        "# 审批\n" + "审批 分级 预算 记忆 会话 部署 " * 100, encoding="utf-8")
    out = rag.search_docs("审批", embed_fn=fake_embed, docs=docs,
                          index_file=tmp_path / "idx.json", workspace=tmp_path)
    assert len(out) <= rag.RESULT_MAX_CHARS + 100  # 含标题行，约 2000 封顶


# ---------- 失败行为 ----------

def test_search_embed_failure_returns_text_not_exception(tmp_path):
    docs = _write_docs(tmp_path)

    def boom(texts):
        raise RuntimeError("接口挂了")

    out = rag.search_docs("审批", embed_fn=boom, docs=docs,
                          index_file=tmp_path / "idx.json", workspace=tmp_path)
    assert out.startswith("工具执行失败")


def test_embed_without_key_raises_rag_error(monkeypatch):
    monkeypatch.setattr(rag, "LLM_API_KEY", "")
    try:
        rag.embed_texts(["x"])
        raise AssertionError("无 Key 时应抛 RagError")
    except rag.RagError as e:
        assert "LLM_API_KEY" in str(e)


def test_search_without_key_returns_failure_text(tmp_path, monkeypatch):
    monkeypatch.setattr(rag, "LLM_API_KEY", "")
    docs = _write_docs(tmp_path)
    out = rag.search_docs("审批", docs=docs,
                          index_file=tmp_path / "idx.json", workspace=tmp_path)
    assert out.startswith("工具执行失败") and "LLM_API_KEY" in out


# ---------- 余弦与分级 ----------

def test_cosine():
    assert rag.cosine([1.0, 0.0], [1.0, 0.0]) == 1.0
    assert rag.cosine([1.0, 0.0], [0.0, 1.0]) == 0.0
    assert rag.cosine([0.0, 0.0], [1.0, 0.0]) == 0.0  # 零向量
    assert rag.cosine([1.0], [1.0, 0.0]) == 0.0  # 维度对不上


def test_search_docs_is_low_risk(tmp_path):
    level, _ = classify("search_docs", {"query": "审批"}, tmp_path)
    assert level == LOW
