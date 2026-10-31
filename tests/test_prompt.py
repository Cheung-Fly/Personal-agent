"""SYSTEM_PROMPT 回归（v0.9）：防误删 t09 修法那一句。

背景：v0.8 真机基线唯一失败 t09——让模型把偏好写进 MEMORY.md 时，
它只在对话里给出拟写入内容、没发出 write_file 调用，审批卡未触发。
v0.9 的修法是在 SYSTEM_PROMPT 补一句“直接调工具、不要只在对话里
给内容”。这句是行为契约，删了 t09 大概率复发，所以钉一个回归测试：
语义关键成分缺一即红。不依赖 LLM。python -m pytest tests/ -q

注意：backend.main 必须在测试函数内导入（与 test_context/test_memory
同一口径）——它在导入时绑定 tools.WORKSPACE 的引用，收集期顶层导入
会让 test_approval 里靠 monkeypatch 换工作区的用例失效。
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("WORKSPACE_DIR", "./agent_workspace_test")


def test_prompt_directs_write_file_calls():
    """写入文件要直接调用 write_file，而不是只在对话里给拟写内容。"""
    from backend.main import SYSTEM_PROMPT

    assert "直接调用 write_file" in SYSTEM_PROMPT
    assert "不要只在对话里给出拟写入内容" in SYSTEM_PROMPT


def test_prompt_mentions_approval_card_handles_consent():
    """直接调工具不等于跳过审批：确认由审批卡处理，不必先在对话里征求同意。"""
    from backend.main import SYSTEM_PROMPT

    assert "审批卡" in SYSTEM_PROMPT
    assert "不用在对话里先征求同意" in SYSTEM_PROMPT
