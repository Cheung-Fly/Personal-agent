"""长期记忆：agent_workspace/MEMORY.md 注入（v0.4）。

设计口径（见设计草稿）：
- 单个全局文件，不建新表、不新增依赖；MEMORY.md 进 git，换机器也跟着走。
- 每次组装上下文时读一次文件（不缓存），改完下一句话就生效。
- 作为第二条 system 消息插在 SYSTEM_PROMPT 之后、history 之前：
  [system, memory, *history, user] —— 双层记忆：SQLite 是短期会话
  记忆，MEMORY.md 是长期偏好记忆。
- 注入上限约 2000 字符（MEMORY_MAX_CHARS 可配），超了只取前部；
  文件缺失或为空 = 不注入、不报错，行为与 v0.3 完全一致。
- v0.4 只开手动入口：Agent 不自动写记忆。write_file 写 MEMORY.md
  会被工具层直接拒绝（见 tools.py），这个口子留给审批分级那版再开。
- v0.5 兑现了上面那个口子：Agent 可以提议写 MEMORY.md，但永远走
  中风险审批卡，用户点了通过才落盘；不通过就维持手动入口不变。
"""
import os
from pathlib import Path

from .tools import WORKSPACE

MEMORY_FILE_NAME = "MEMORY.md"
MEMORY_MAX_CHARS = int(os.environ.get("MEMORY_MAX_CHARS", "2000"))

MEMORY_HEADER = "以下是用户的长期记忆（偏好与结论，写在 agent_workspace/MEMORY.md 里，可手动编辑；想改写时可提议，须经用户在审批卡上确认后才生效），请在回复中遵守："


def memory_path(workspace: Path | None = None) -> Path:
    return (workspace or WORKSPACE) / MEMORY_FILE_NAME


def load_long_term_memory(workspace: Path | None = None) -> str:
    """读长期记忆全文；缺失/为空返回 ""；超长截断到 MEMORY_MAX_CHARS。"""
    p = memory_path(workspace)
    try:
        text = p.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return ""
    if not text:
        return ""
    if len(text) > MEMORY_MAX_CHARS:
        print(f"[memory] MEMORY.md 超长（{len(text)} 字符），只注入前 {MEMORY_MAX_CHARS} 字符")
        return text[:MEMORY_MAX_CHARS]
    return text


def memory_system_message(workspace: Path | None = None) -> dict | None:
    """有记忆时返回第二条 system 消息，否则返回 None。"""
    text = load_long_term_memory(workspace)
    if not text:
        return None
    return {"role": "system", "content": f"{MEMORY_HEADER}\n\n{text}"}
