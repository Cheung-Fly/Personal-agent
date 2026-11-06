"""长期记忆 v0.14 测试：条目化存储、关键词检索、文件同步、迁移分类。

口径：条目表是权威存储、MEMORY.md 是写入落点与导出；写入仍走
“提议 + 审批”（未审批落不了库）；检索没命中不注入。
python -m pytest tests/ -q"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("WORKSPACE_DIR", "./agent_workspace_test")

from backend.memory import (  # noqa: E402
    list_memory_items,
    plan_migration,
    retrieve_memory,
    split_memory_entries,
    sync_memory_from_file,
)
from backend.tools import run_tool  # noqa: E402


def _db(tmp_path):
    return tmp_path / "data" / "agent.db"


SAMPLE = """# 长期记忆

<!-- 注释行，不是事实 -->

- 回复默认用中文，简洁直接。
- 我的幸运数字是 7391。
我家在成都，平时喝美式咖啡，周末喜欢爬山。
"""


def test_split_classifies_lines():
    kinds = [(e["text"], e["kind"]) for e in split_memory_entries(SAMPLE) if e["raw"].strip()]
    assert ("", "skip") in kinds  # 标题行
    assert ("回复默认用中文，简洁直接。", "item") in kinds
    assert ("我的幸运数字是 7391。", "item") in kinds
    # 一行三事实（多个句末标点）→ pending，迁移只列清单不迁入
    assert any(k == "pending" and "成都" in t for t, k in kinds)


def test_plan_migration_counts():
    plan = plan_migration(SAMPLE)
    assert len(plan["migrate"]) == 2
    assert len(plan["pending"]) == 1
    assert any("长期记忆" in s for s in plan["skipped"])


def test_sync_add_keep_supersede(tmp_path):
    db = _db(tmp_path)
    (tmp_path / "MEMORY.md").write_text(
        "- 回复默认用中文。\n- 我的幸运数字是 7391。\n", encoding="utf-8")
    s1 = sync_memory_from_file(tmp_path, db, source="migrated")
    assert s1["added"] == 2 and s1["superseded"] == 0
    first = {i["text"]: i for i in list_memory_items(db)}
    # 改数字、删中文偏好：旧数字条 superseded，新数字进表，中文偏好 superseded
    (tmp_path / "MEMORY.md").write_text("- 我的幸运数字是 8888。\n", encoding="utf-8")
    s2 = sync_memory_from_file(tmp_path, db, source="conversation")
    assert s2["added"] == 1 and s2["superseded"] == 2
    active = [i["text"] for i in list_memory_items(db)]
    assert active == ["我的幸运数字是 8888。"]
    all_items = list_memory_items(db, include_superseded=True)
    old = [i for i in all_items if i["text"] == "我的幸运数字是 7391。"][0]
    assert old["status"] == "superseded" and old["superseded_at"]
    assert old["source"] == "migrated"  # 旧条目的来源/时间不漂移
    assert first["我的幸运数字是 7391。"]["id"] == old["id"]


def test_sync_idempotent_when_file_unchanged(tmp_path):
    db = _db(tmp_path)
    (tmp_path / "MEMORY.md").write_text("- 回复默认用中文。\n", encoding="utf-8")
    sync_memory_from_file(tmp_path, db)
    s = sync_memory_from_file(tmp_path, db)
    assert s["added"] == 0 and s["superseded"] == 0 and s["kept"] == 1


def test_migration_apply_skips_pending(tmp_path):
    db = _db(tmp_path)
    (tmp_path / "MEMORY.md").write_text(SAMPLE, encoding="utf-8")
    s = sync_memory_from_file(tmp_path, db, source="migrated", only_unambiguous=True)
    assert s["added"] == 2 and s["pending"] == 1
    texts = [i["text"] for i in list_memory_items(db)]
    assert not any("成都" in t for t in texts)


def test_retrieve_hit_and_miss(tmp_path):
    db = _db(tmp_path)
    (tmp_path / "MEMORY.md").write_text(
        "- 我的幸运数字是 7391。\n- 回复默认用中文，简洁直接。\n", encoding="utf-8")
    sync_memory_from_file(tmp_path, db)
    hits = retrieve_memory("我的幸运数字是多少？", db)
    assert hits and "7391" in hits[0]["text"]
    # 反向用例：无关问题不命中任何条目
    assert retrieve_memory("请解释快速排序的原理", db) == []
    assert retrieve_memory("", db) == []


def test_retrieve_topk_and_budget(tmp_path):
    db = _db(tmp_path)
    lines = "".join(f"- 数字偏好第{i}条是 {i}。\n" for i in range(8))
    (tmp_path / "MEMORY.md").write_text(lines, encoding="utf-8")
    sync_memory_from_file(tmp_path, db)
    hits = retrieve_memory("数字偏好是几？", db, top_k=3)
    assert len(hits) == 3
    hits = retrieve_memory("数字偏好是几？", db, max_chars=30)
    total = sum(len(h["_line"]) for h in hits)
    assert total <= 30  # 预算封顶：第一条截断收下，其余不再塞


def test_write_through_approval_lands_in_items(tmp_path, monkeypatch):
    """写入通道不变：未审批写不进；审批通过落盘后条目即时同步。"""
    monkeypatch.setattr("backend.tools.WORKSPACE", tmp_path)
    monkeypatch.setattr("backend.memory.WORKSPACE", tmp_path)
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    out = run_tool("write_file", {"path": "MEMORY.md", "content": "- 我的幸运数字是 7391。\n"})
    assert "手动编辑" in out
    assert list_memory_items(_db(tmp_path)) == []
    out = run_tool("write_file",
                   {"path": "MEMORY.md", "content": "- 我的幸运数字是 7391。\n"},
                   approved=True)
    assert out.startswith("已写入")
    items = list_memory_items(_db(tmp_path))
    assert [i["text"] for i in items] == ["我的幸运数字是 7391。"]
    assert items[0]["source"] == "conversation"
