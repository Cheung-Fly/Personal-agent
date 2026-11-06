#!/usr/bin/env python3
"""v0.14 长期记忆迁移工具：把 MEMORY.md 按条目拆分迁入条目表。

默认 dry-run：只打印“迁入 / 待确认 / 丢弃”清单，不落库。
--apply 才真正迁入，且只迁无歧义条目（pending 只列清单不迁入，
等人工确认）；MEMORY.md 原文件不动（它此后是可读导出）。

用法（在仓库根目录，靠环境变量定位工作区与数据库）：
  WORKSPACE_DIR=./agent_workspace DATA_DIR=./data \
    python3 scripts/migrate_memory_v014.py [--apply]

已迁过（条目表里已有条目）会拒绝重复 apply，避免双份事实。
"""
import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.memory import (  # noqa: E402
    _read_file_raw,
    list_memory_items,
    memory_path,
    plan_migration,
    sync_memory_from_file,
)


def main() -> int:
    ap = argparse.ArgumentParser(description="v0.14 长期记忆条目化迁移")
    ap.add_argument("--apply", action="store_true", help="真正落库（默认只出清单）")
    args = ap.parse_args()

    text = _read_file_raw()
    plan = plan_migration(text)
    print(f"MEMORY.md：{memory_path()}")
    print(f"迁入（无歧义）{len(plan['migrate'])} 条：")
    for t in plan["migrate"]:
        print(f"  + {t}")
    print(f"待确认（不迁入）{len(plan['pending'])} 行：")
    for t in plan["pending"]:
        print(f"  ? {t}")
    print(f"丢弃（结构行）{len(plan['skipped'])} 行：")
    for t in plan["skipped"]:
        print(f"  - {t!r}")

    if not args.apply:
        print("\n[dry-run] 未落库。确认清单无误后加 --apply 执行迁入。")
        return 0
    existing = list_memory_items(include_superseded=True)
    if existing:
        print(f"\n条目表里已有 {len(existing)} 条，拒绝重复迁移。")
        return 1
    stats = sync_memory_from_file(source="migrated", only_unambiguous=True)
    print(f"\n已迁入：新增 {stats['added']}、取代 {stats['superseded']}、"
          f"待确认跳过 {stats['pending']}、结构行跳过 {stats['skipped']}")
    for item in list_memory_items():
        print(f"  #{item['id']} [{item['source']}] {item['text']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
