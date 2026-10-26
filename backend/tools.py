"""工具执行层：所有操作限制在 WORKSPACE_DIR 沙箱内。"""
import os
import subprocess
from pathlib import Path

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


def run_tool(name: str, args: dict) -> str:
    try:
        if name == "list_files":
            p = _safe_path(args.get("path", "."))
            items = sorted(str(x.relative_to(WORKSPACE)) for x in p.rglob("*") if ".git" not in x.parts)
            return "\n".join(items[:500]) or "(空)"
        if name == "read_file":
            return _safe_path(args["path"]).read_text(encoding="utf-8")[:20000]
        if name == "write_file":
            p = _safe_path(args["path"])
            # v0.4 只开手动入口：长期记忆 MEMORY.md 请手动编辑，Agent
            # 不能直接改写（放开自动写等于 Agent 能改自己的记忆，写错会
            # 污染以后所有对话；这个口子留给审批分级那版再开）。
            if p == WORKSPACE / "MEMORY.md":
                return "工具执行失败: 长期记忆 MEMORY.md 请手动编辑，Agent 不能直接改写（v0.4 只开手动入口）"
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
