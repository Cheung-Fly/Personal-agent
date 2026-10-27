"""工具执行层：所有操作限制在 WORKSPACE_DIR 沙箱内。"""
import os
import subprocess
from pathlib import Path

from .approval import HIGH, classify

WORKSPACE = Path(os.environ.get("WORKSPACE_DIR", "./agent_workspace")).resolve()
WORKSPACE.mkdir(parents=True, exist_ok=True)

# 给模型的工具 Schema（OpenAI-compatible function calling 格式）
TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "列出工作区内的文件和目录",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "相对工作区的路径，默认 '.'"}},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "读取工作区内一个文本文件的内容",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "把内容写入工作区内的文件（覆盖），写代码主要用这个",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "exec_command",
            "description": "在工作区目录里执行一条 shell 命令，如 ls、python、pytest、git status。有 60 秒超时。",
            "parameters": {
                "type": "object",
                "properties": {"command": {"type": "string"}},
                "required": ["command"],
            },
        },
    },
]


def _safe_path(rel: str) -> Path:
    p = (WORKSPACE / rel).resolve()
    if WORKSPACE not in p.parents and p != WORKSPACE:
        raise ValueError(f"路径越界，拒绝访问: {rel}")
    return p


def run_tool(name: str, args: dict, approved: bool = False) -> str:
    """执行一次工具调用。approved=True 表示这次调用已经过审批卡
    （只用于 MEMORY.md 写这种中风险调用被用户点通过的情况）。

    纵深防御：即使直接调 run_tool，高风险调用也会在这里被拦住——
    主流程（main.py）先分级再决定要不要走到这里，但工具层自己也
    不放行高危命令 / 敏感路径；MEMORY.md 没经过审批也写不了。"""
    try:
        if name in ("write_file", "exec_command"):
            level, reason = classify(name, args, WORKSPACE)
            if level == HIGH:
                return f"工具执行失败: {name} 调用被拒绝（高风险：{reason}）"
        if name == "list_files":
            p = _safe_path(args.get("path", "."))
            items = sorted(str(x.relative_to(WORKSPACE)) for x in p.rglob("*") if ".git" not in x.parts)
            return "\n".join(items[:500]) or "(空)"
        if name == "read_file":
            return _safe_path(args["path"]).read_text(encoding="utf-8")[:20000]
        if name == "write_file":
            p = _safe_path(args["path"])
            # v0.5 审批卡开一半：长期记忆 MEMORY.md 可以由 Agent 提议写，
            # 但必须经用户在审批卡上点通过（approved=True）才落盘；没批
            # 就还是 v0.4 的手动入口。直接调 run_tool 永远 approved=False，
            # 所以这道口子不会被绕过。
            if p == WORKSPACE / "MEMORY.md" and not approved:
                return "工具执行失败: 长期记忆 MEMORY.md 请手动编辑，或在流式对话里经审批通过后改写（v0.5 审批卡）"
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(args["content"], encoding="utf-8")
            return f"已写入 {args['path']} ({len(args['content'])} 字符)"
        if name == "exec_command":
            r = subprocess.run(
                args["command"], shell=True, cwd=WORKSPACE,
                capture_output=True, text=True, timeout=60,
            )
            return f"exit={r.returncode}\nstdout:\n{r.stdout[:10000]}\nstderr:\n{r.stderr[:5000]}"
        return f"未知工具: {name}"
    except Exception as e:  # 工具失败也要回喂给模型，让它自己纠错
        return f"工具执行失败: {type(e).__name__}: {e}"
