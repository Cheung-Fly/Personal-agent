"""最小 MCP 演示 Server（v0.13）：标准库 stdio JSON-RPC，给主项目的
MCP Client 当第一个真实协议对端。

定位要诚实（见 v0.13 设计草稿）：它是一个真实的 MCP 协议对端——
独立子进程、真实 initialize 握手与 tools/call——但代码在自家仓库
里，它的存在只证明“客户端与一个按协议实现的 Server 互通”，不
等于接通了任何第三方 Server，更不等于接通了生图工坊。

启动：python -m backend.mcp_demo（stdio 传输，stdout 只走协议，
任何日志只能打 stderr，别污染协议通道）。

演示工具 3 个，配合 .env 里 MCP_SERVERS_JSON 的 risk 配置覆盖
三档审批链路（分级在客户端注册表里，不在这个 Server 里）：
- demo_time        返回 Server 当前时间，只读 → 配置 low 自动执行
- demo_append_note 往演示文件追加一行       → 配置 medium 弹卡
- demo_delete_note 删除演示文件             → 配置 high 默认拒绝
演示文件路径由 MCP_DEMO_FILE 指定，默认 data/mcp_demo_notes.txt
（相对 Server 进程的工作目录）。
"""
import json
import os
import sys
from datetime import datetime, timezone

DEMO_FILE = os.environ.get("MCP_DEMO_FILE", "data/mcp_demo_notes.txt")

TOOLS = [
    {
        "name": "demo_time",
        "description": "返回 MCP 演示 Server 的当前时间（只读演示工具）",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "demo_append_note",
        "description": "往演示文件末尾追加一行文本（演示写操作，会改文件）",
        "inputSchema": {
            "type": "object",
            "properties": {"text": {"type": "string", "description": "要追加的一行文本"}},
            "required": ["text"],
        },
    },
    {
        "name": "demo_delete_note",
        "description": "删除整个演示文件（演示危险操作）",
        "inputSchema": {"type": "object", "properties": {}},
    },
]


def _text_result(text: str, is_error: bool = False) -> dict:
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


def _call_tool(name: str, arguments: dict) -> dict:
    if name == "demo_time":
        now = datetime.now(timezone.utc).isoformat()
        return _text_result(f"演示 Server 当前时间（UTC）：{now}")
    if name == "demo_append_note":
        text = str((arguments or {}).get("text") or "")
        path = os.path.abspath(DEMO_FILE)
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(text + "\n")
        return _text_result(f"已向演示文件追加一行：{text}（文件：{path}）")
    if name == "demo_delete_note":
        path = os.path.abspath(DEMO_FILE)
        if os.path.exists(path):
            os.remove(path)
            return _text_result(f"演示文件已删除：{path}")
        return _text_result(f"演示文件不存在，无需删除：{path}", is_error=True)
    return _text_result(f"未知工具: {name}", is_error=True)


def handle(msg: dict) -> dict | None:
    """处理一条 JSON-RPC 消息；通知（无 id）不回响应，返回 None。"""
    method = msg.get("method") or ""
    msg_id = msg.get("id")
    if method == "initialize":
        params = msg.get("params") or {}
        return {"jsonrpc": "2.0", "id": msg_id, "result": {
            "protocolVersion": params.get("protocolVersion") or "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "personal-agent-mcp-demo", "version": "0.13.0"},
        }}
    if method.startswith("notifications/"):
        return None
    if msg_id is None:
        return None
    if method == "ping":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {"tools": TOOLS}}
    if method == "tools/call":
        params = msg.get("params") or {}
        result = _call_tool(str(params.get("name") or ""), params.get("arguments") or {})
        return {"jsonrpc": "2.0", "id": msg_id, "result": result}
    return {"jsonrpc": "2.0", "id": msg_id,
            "error": {"code": -32601, "message": f"Method not found: {method}"}}


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue  # 协议通道上的坏行直接丢，别让一行脏数据杀掉 Server
        if not isinstance(msg, dict):
            continue
        response = handle(msg)
        if response is not None:
            sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
