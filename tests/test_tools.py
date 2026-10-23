"""不依赖 LLM 的工具层测试： python -m pytest tests/ -q"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("WORKSPACE_DIR", "./agent_workspace_test")

from backend.tools import run_tool  # noqa: E402


def test_write_read_exec_and_sandbox():
    assert "已写入" in run_tool("write_file", {"path": "a.py", "content": "print(42)"})
    assert "print(42)" in run_tool("read_file", {"path": "a.py"})
    out = run_tool("exec_command", {"command": "python3 a.py"})
    assert "42" in out and "exit=0" in out
    # 路径穿越必须被拦
    assert "失败" in run_tool("read_file", {"path": "../secret.txt"})
    assert "a.py" in run_tool("list_files", {})
