"""v0.13 MCP 工具接入单测。

背景：主项目做 MCP Client（stdio-only，纯标准库），工具经注册表
进工具表、经 approval.classify 分级、经 run_tool 分发。这里验的是
机制，不是任何真实外部 Server：
- 名字空间化 / 清洗 / 超长跳过 / 冲突先胜 / allow 白名单；
- schema 转换（缺 inputSchema 给空对象 schema）；
- 风险定级：默认 MEDIUM、配置覆盖（原始名与全名都认）、非法值
  回落、未注册的 mcp_ 名在 classify 里仍是 HIGH；
- 客户端环境最小化：LLM_API_KEY 不进子进程 env；
- 对仓库自带演示 Server 的真实 stdio 全程：握手、tools/list、
  tools/call 三个工具都走通；坏 Server（不存在的命令 / 立刻退出）
  不阻塞注册表启动；非法 JSON 配置被 start_from_env 忽略；
- run_tool 纵深防御：medium 没 approved 不执行、high 给 approved
  也不执行、low 直接执行。
"""
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("WORKSPACE_DIR", "./agent_workspace_test")

import pytest  # noqa: E402

from backend import mcp  # noqa: E402
from backend.approval import HIGH, LOW, MEDIUM, classify  # noqa: E402
from backend.tools import WORKSPACE, all_tool_schemas, run_tool  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]

DEMO_TOOLS = [
    {"name": "demo_time", "description": "时间",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "demo_append_note", "description": "追加",
     "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}},
                     "required": ["text"]}},
    {"name": "demo_delete_note", "description": "删除",
     "inputSchema": {"type": "object", "properties": {}}},
]


@pytest.fixture()
def fresh_registry(monkeypatch):
    """把单例 REGISTRY 换成空注册表，测试互不污染；结束关子进程。"""
    registry = mcp.MCPRegistry()
    monkeypatch.setattr(mcp, "REGISTRY", registry)
    yield registry
    registry.close_all()


def demo_config(tmp_path):
    return {
        "name": "demo",
        "command": [sys.executable, "-m", "backend.mcp_demo"],
        "env": {"MCP_DEMO_FILE": str(tmp_path / "notes.txt")},
        "risk": {"demo_time": "low", "demo_append_note": "medium",
                 "demo_delete_note": "high"},
    }


# ---------------- 名字与注册 ----------------

def test_sanitize_and_namespace():
    assert mcp.sanitize_part("my server!") == "my_server_"
    assert mcp.sanitize_part("ok-name_1") == "ok-name_1"
    assert mcp.sanitize_part("") == "_"
    assert mcp.namespaced_name("demo", "demo_time") == "mcp_demo_demo_time"
    assert mcp.namespaced_name("a b", "c/d") == "mcp_a_b_c_d"


def test_register_defaults_medium_and_schema_conversion(fresh_registry):
    count = fresh_registry.register_tools("demo", DEMO_TOOLS)
    assert count == 3
    entry = fresh_registry.entries["mcp_demo_demo_time"]
    assert entry["risk"] == MEDIUM  # 没配 risk → 默认中风险
    schemas = {s["function"]["name"]: s["function"]
               for s in fresh_registry.schemas()}
    assert schemas["mcp_demo_demo_append_note"]["parameters"]["required"] == ["text"]
    # 缺 inputSchema 的工具给空对象 schema，不许把 None 发给模型
    fresh_registry.register_tools("bare", [{"name": "no_schema"}])
    bare = {s["function"]["name"]: s["function"]
            for s in fresh_registry.schemas()}["mcp_bare_no_schema"]
    assert bare["parameters"] == {"type": "object", "properties": {}}


def test_register_risk_override_and_invalid_fallback(fresh_registry):
    fresh_registry.register_tools(
        "demo", DEMO_TOOLS,
        risk_map={"demo_time": "low", "mcp_demo_demo_append_note": "high",
                  "demo_delete_note": "bogus"})
    assert fresh_registry.risk_for("mcp_demo_demo_time") == LOW  # 原始名覆盖
    assert fresh_registry.risk_for("mcp_demo_demo_append_note") == HIGH  # 全名覆盖
    assert fresh_registry.risk_for("mcp_demo_demo_delete_note") == MEDIUM  # 非法值回落


def test_register_allow_whitelist_and_conflict_first_wins(fresh_registry):
    assert fresh_registry.register_tools("demo", DEMO_TOOLS,
                                         allow=["demo_time"]) == 1
    assert "mcp_demo_demo_append_note" not in fresh_registry.entries
    # 再注册同一批：新工具照常进表，唯独重名的 demo_time 被跳过，
    # 且保留第一次注册的条目（先注册者胜出，不被覆盖）
    assert fresh_registry.register_tools(
        "demo", DEMO_TOOLS, risk_map={"demo_time": "low"}) == 2
    assert fresh_registry.risk_for("mcp_demo_demo_time") == MEDIUM


def test_register_overlong_name_skipped_not_truncated(fresh_registry):
    long_tool = {"name": "t" * 40, "description": "", "inputSchema": {}}
    assert fresh_registry.register_tools("s" * 30, [long_tool]) == 0
    assert fresh_registry.entries == {}


# ---------------- 分级（approval.classify 的 MCP 分支） ----------------

def test_classify_mcp_registered_and_unregistered(fresh_registry):
    fresh_registry.register_tools("demo", DEMO_TOOLS,
                                  risk_map={"demo_time": "low"})
    level, _ = classify("mcp_demo_demo_time", {}, WORKSPACE)
    assert level == LOW
    level, _ = classify("mcp_demo_demo_append_note", {}, WORKSPACE)
    assert level == MEDIUM  # 默认
    # 带 mcp_ 前缀但没注册成功 / 模型编出来的名字：未知工具 = HIGH
    level, _ = classify("mcp_demo_not_a_tool", {}, WORKSPACE)
    assert level == HIGH
    level, _ = classify("mcp_evil_thing", {}, WORKSPACE)
    assert level == HIGH


# ---------------- 客户端环境与健壮性 ----------------

def test_client_env_excludes_llm_key(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "sk-should-never-leak")
    monkeypatch.setenv("ACCESS_TOKEN", "should-not-leak-either")
    client = mcp.MCPClient("x", ["python"], {"EXPLICIT": "1"})
    env = client._build_env()
    assert "LLM_API_KEY" not in env
    assert "ACCESS_TOKEN" not in env
    assert env["EXPLICIT"] == "1"
    assert "PATH" in env


def test_registry_start_survives_bad_servers(fresh_registry):
    fresh_registry.start([
        {"name": "ghost", "command": ["definitely-not-a-real-binary-v013"]},
        {"name": "quitter", "command": [sys.executable, "-c", "pass"]},
        {"name": "broken"},  # 缺 command
        "not-a-dict",
    ])
    assert fresh_registry.entries == {}
    assert fresh_registry.clients == {}


def test_start_from_env_ignores_bad_json(monkeypatch, fresh_registry):
    monkeypatch.setenv("MCP_SERVERS_JSON", "{not json")
    assert mcp.start_from_env() is fresh_registry
    assert fresh_registry.entries == {}
    monkeypatch.setenv("MCP_SERVERS_JSON", "")
    assert mcp.start_from_env() is fresh_registry


def test_client_request_timeout():
    client = mcp.MCPClient("sleeper", [sys.executable, "-c",
                                       "import time; time.sleep(30)"])
    try:
        client.start(timeout=1)
        pytest.fail("睡着的 server 不该握手成功")
    except mcp.MCPError as e:
        assert "超时" in str(e)
    finally:
        client.close()


# ---------------- 对演示 Server 的真实 stdio 全程 ----------------

@pytest.fixture()
def live_demo(fresh_registry, tmp_path):
    fresh_registry.start([demo_config(tmp_path)],
                         reserved_names={"list_files", "read_file"})
    assert set(fresh_registry.clients) == {"demo"}
    return fresh_registry, tmp_path / "notes.txt"


def test_demo_server_full_roundtrip(live_demo):
    registry, notes = live_demo
    names = {s["function"]["name"] for s in registry.schemas()}
    assert names == {"mcp_demo_demo_time", "mcp_demo_demo_append_note",
                     "mcp_demo_demo_delete_note"}
    out = registry.call("mcp_demo_demo_time", {})
    assert "时间" in out
    out = registry.call("mcp_demo_demo_append_note", {"text": "第一行"})
    assert "已向演示文件追加" in out
    assert notes.read_text(encoding="utf-8") == "第一行\n"
    # 合并工具表：内置 5 + MCP 3
    merged = {s["function"]["name"] for s in all_tool_schemas()}
    assert "mcp_demo_demo_time" in merged and "write_file" in merged


def test_run_tool_defense_layers(live_demo):
    registry, notes = live_demo
    # low：直接经 run_tool 执行
    out = run_tool("mcp_demo_demo_time", {})
    assert "时间" in out
    # medium 没经审批：run_tool 自己拦下，文件绝不能被写
    out = run_tool("mcp_demo_demo_append_note", {"text": "不该落盘"})
    assert "失败" in out and not notes.exists()
    # medium 经审批：才真正执行
    out = run_tool("mcp_demo_demo_append_note", {"text": "审批后的一行"},
                   approved=True)
    assert "已向演示文件追加" in out
    assert notes.read_text(encoding="utf-8") == "审批后的一行\n"
    # high：就算 approved=True 也不执行（主流程根本不会放它到这）
    out = run_tool("mcp_demo_demo_delete_note", {}, approved=True)
    assert "失败" in out and notes.exists()


def test_demo_server_subprocess_survives_client_close(tmp_path):
    """关掉客户端后子进程必须被收掉，不能留僵尸。"""
    registry = mcp.MCPRegistry()
    registry.start([demo_config(tmp_path)])
    proc = registry.clients["demo"]._proc
    registry.close_all()
    assert proc.poll() is not None
