"""MCP 工具接入（v0.13）：主项目做 MCP Client，本版只做 stdio 传输。

设计口径（见设计草稿 v0.13-MCP工具接入设计草稿.md，Chung 定案 A）：
- 协议是 MCP 的 stdio 传输：子进程 + JSON-RPC 2.0 走 stdin/stdout，
  每行一条消息：initialize → notifications/initialized → tools/list
  → tools/call。纯标准库（subprocess + 线程读行），零新增依赖，
  Dockerfile 不动；SSE 传输本版不做。
- 实现选同步客户端而不是 asyncio 子进程：现有工具执行层 run_tool
  本来就是同步阻塞的（exec_command 用 subprocess.run 阻塞最长 60
  秒），同步客户端直接嵌进同一条执行路径，不为了一个协议层把整个
  工具层改异步。同一 Server 的调用用锁串行化——Agent 循环目前也是
  顺序执行，工具并行是 v0.16 的事。
- 配置即信任边界（必须说清，不能含糊）：Server 命令只来自运维者
  自己的 .env（MCP_SERVERS_JSON），模型和对话用户都不能注入新
  Server；MCP 工具**不受** tools._safe_path 的工作区沙箱约束——
  外部 Server 的代码能干什么取决于它自己。所以注册表有 allow
  白名单、审批默认 MEDIUM（见 approval.classify 的 MCP 分支），
  接一个不了解的 Server 前先只放行点过名的工具。
- 子进程环境默认最小化：只给 PATH + 配置里显式写的 env，绝不把
  整份 os.environ 透传——里面有 LLM_API_KEY、ACCESS_TOKEN，Server
  没有理由拿到它们。
- 启动韧性：没配 MCP_SERVERS_JSON、配置写错、某个 Server 握手
  失败，都只记一行日志然后跳过，应用照常启动、只有内置工具。
  外部 Server 起不来不能拖垮 Agent 本体。
"""
import json
import os
import queue
import re
import subprocess
import threading

PROTOCOL_VERSION = "2024-11-05"
CLIENT_INFO = {"name": "personal-agent", "version": "0.13.0"}
HANDSHAKE_TIMEOUT_SECONDS = 10
CALL_TIMEOUT_SECONDS = 60  # 与 exec_command 的 60 秒超时对齐
MAX_TOOL_NAME_LENGTH = 64  # OpenAI function 名上限；超长不截断（截断可能撞名），整个跳过

LEVELS = ("low", "medium", "high")
DEFAULT_RISK = "medium"  # MCP 工具默认中风险（弹卡），配置 risk 可逐工具覆盖

_SANITIZE_RE = re.compile(r"[^A-Za-z0-9_-]")


class MCPError(Exception):
    """MCP 协议层失败：握手超时、子进程退出、Server 返回 error 等。
    调用方（run_tool）会把它转成“工具执行失败”文本回喂模型。"""


def sanitize_part(part: str) -> str:
    """名字空间化的一段：只保留 [A-Za-z0-9_-]，其余字符转下划线。"""
    cleaned = _SANITIZE_RE.sub("_", str(part))
    return cleaned or "_"


def namespaced_name(server_name: str, tool_name: str) -> str:
    """MCP 工具注册进工具表后的全名：mcp_<server>_<tool>。"""
    return f"mcp_{sanitize_part(server_name)}_{sanitize_part(tool_name)}"


class MCPClient:
    """一个 MCP Server 子进程的 stdio 客户端。

    请求/响应靠一个后台读行线程 + 队列配对：stdout 每行一条 JSON，
    读线程把消息塞进队列，_request 按 id 等自己的响应（带超时）。
    Server 主动发的通知/请求本版不处理，读到直接忽略（演示与常见
    只读 Server 都不会发；真遇到会发请求的 Server 是以后的事）。
    """

    def __init__(self, name: str, command: list[str], env_extra: dict | None = None):
        self.name = name
        self.command = [str(c) for c in command]
        self.env_extra = {str(k): str(v) for k, v in (env_extra or {}).items()}
        self._proc: subprocess.Popen | None = None
        self._queue: queue.Queue = queue.Queue()
        self._lock = threading.Lock()  # 同一 Server 的请求串行化
        self._next_id = 0
        self._dead = False

    # ---------- 进程与环境 ----------

    def _build_env(self) -> dict:
        """子进程环境：PATH + 配置显式给的 env，仅此而已。

        绝不透传 os.environ——里面有 LLM_API_KEY / ACCESS_TOKEN 等
        凭据，Server 子进程没有理由拿到（信任边界，见模块头）。"""
        env = {"PATH": os.environ.get("PATH", "")}
        env.update(self.env_extra)
        return env

    def start(self, timeout: float = HANDSHAKE_TIMEOUT_SECONDS) -> dict:
        """拉起子进程并完成 initialize 握手，返回 Server 的握手结果。
        任何一步失败都抛 MCPError，由注册表跳过这个 Server。"""
        if self._proc is not None:
            raise MCPError(f"MCP server {self.name} 已启动")
        try:
            self._proc = subprocess.Popen(
                self.command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                # stderr 不接管：继承父进程，Server 的报错直接进
                # 容器/服务日志，也不会因管道没人读而堵死子进程。
                text=True,
                bufsize=1,
                env=self._build_env(),
            )
        except OSError as e:
            raise MCPError(f"MCP server {self.name} 启动失败: {e}") from e
        threading.Thread(target=self._read_loop, daemon=True).start()
        result = self._request("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": CLIENT_INFO,
        }, timeout)
        self._notify("notifications/initialized", {})
        return result if isinstance(result, dict) else {}

    def close(self) -> None:
        """关子进程：先 terminate，给 3 秒优雅退出，不行再 kill。
        应用关闭时由注册表统一调，别留僵尸进程。"""
        proc, self._proc = self._proc, None
        self._dead = True
        if proc is None:
            return
        try:
            if proc.stdin:
                proc.stdin.close()
        except OSError:
            pass
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()

    # ---------- 协议收发 ----------

    def _read_loop(self) -> None:
        """后台线程：逐行读 stdout 解析 JSON 塞进队列；读到 EOF（进程
        退出）塞一个 None 哨兵，让等响应的 _request 立刻失败而不是
        干等到超时。非法 JSON 行直接丢弃，不许一行坏数据杀掉线程。"""
        try:
            for line in self._proc.stdout:  # type: ignore[union-attr]
                line = line.strip()
                if not line:
                    continue
                try:
                    self._queue.put(json.loads(line))
                except json.JSONDecodeError:
                    continue
        except (OSError, ValueError):
            pass
        self._queue.put(None)

    def _send(self, payload: dict) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None or self._dead:
            raise MCPError(f"MCP server {self.name} 未运行")
        try:
            proc.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
            proc.stdin.flush()
        except (BrokenPipeError, OSError) as e:
            self._dead = True
            raise MCPError(f"MCP server {self.name} 已退出: {e}") from e

    def _notify(self, method: str, params: dict) -> None:
        with self._lock:
            self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def _request(self, method: str, params: dict, timeout: float):
        with self._lock:
            if self._dead or self._proc is None:
                raise MCPError(f"MCP server {self.name} 未运行")
            self._next_id += 1
            request_id = self._next_id
            self._send({"jsonrpc": "2.0", "id": request_id,
                        "method": method, "params": params})
            return self._wait_response(request_id, timeout, method)

    def _wait_response(self, request_id: int, timeout: float, method: str):
        import time
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise MCPError(f"MCP {method} 超时（{timeout}s）: server {self.name}")
            try:
                msg = self._queue.get(timeout=remaining)
            except queue.Empty:
                raise MCPError(f"MCP {method} 超时（{timeout}s）: server {self.name}") from None
            if msg is None:
                self._dead = True
                raise MCPError(f"MCP server {self.name} 已退出，{method} 无响应")
            if not isinstance(msg, dict) or msg.get("id") != request_id:
                continue  # 通知或别人的消息，本版不处理
            if "error" in msg:
                err = msg["error"] or {}
                raise MCPError(f"MCP {method} 被 server {self.name} 拒绝: "
                               f"{err.get('message', err)}")
            return msg.get("result")

    # ---------- 工具发现与调用 ----------

    def list_tools(self, timeout: float = HANDSHAKE_TIMEOUT_SECONDS) -> list[dict]:
        result = self._request("tools/list", {}, timeout)
        tools = (result or {}).get("tools") or []
        return [t for t in tools if isinstance(t, dict) and t.get("name")]

    def call_tool(self, tool_name: str, arguments: dict,
                  timeout: float = CALL_TIMEOUT_SECONDS) -> str:
        """调一次 tools/call，返回给模型看的文本。

        result.content 是内容块数组，取 text 块拼接（非 text 块把
        JSON 原样带上，不丢信息）；isError=True 是工具自身的失败，
        抛 MCPError 让 run_tool 按统一的失败文本回喂。"""
        result = self._request("tools/call",
                               {"name": tool_name, "arguments": arguments or {}},
                               timeout)
        result = result or {}
        texts = []
        for block in result.get("content") or []:
            if isinstance(block, dict) and block.get("type") == "text":
                texts.append(str(block.get("text", "")))
            elif isinstance(block, dict):
                texts.append(json.dumps(block, ensure_ascii=False))
        text = "\n".join(texts) or "(MCP 工具无文本返回)"
        if result.get("isError"):
            raise MCPError(text)
        return text[:20000]  # 与 read_file 的返回上限对齐


class MCPRegistry:
    """全部 MCP Server 与其工具的注册表（进程内单例 REGISTRY）。

    条目：全名 -> {server, tool, description, parameters, risk}。
    冲突处理：同名后到者跳过并记日志——先注册的胜出，不静默覆盖；
    内置工具名由启动方经 reserved_names 传入先占位（MCP 名都带
    mcp_ 前缀，其实撞不上内置，这是纵深防御）。"""

    def __init__(self):
        self.entries: dict[str, dict] = {}
        self.clients: dict[str, MCPClient] = {}

    # ---------- 注册 ----------

    def register_tools(self, server_name: str, tools: list[dict],
                       risk_map: dict | None = None,
                       allow: list[str] | None = None) -> int:
        """把一个 Server 的 tools/list 结果注册进表，返回注册数。

        allow 是原始工具名的白名单（不在名单里的不注册）；risk_map
        按工具名定级，原始名与名字空间化全名都认，全名优先，没配的
        默认 MEDIUM，配了非法值也回落 MEDIUM 并记日志。"""
        risk_map = risk_map or {}
        count = 0
        for tool in tools:
            raw_name = str(tool.get("name") or "")
            if not raw_name:
                continue
            if allow is not None and raw_name not in allow:
                print(f"[mcp] 跳过未在 allow 白名单内的工具: {server_name}/{raw_name}")
                continue
            full = namespaced_name(server_name, raw_name)
            if len(full) > MAX_TOOL_NAME_LENGTH:
                print(f"[mcp] 跳过名字超长（>{MAX_TOOL_NAME_LENGTH}）的工具: {full}")
                continue
            if full in self.entries:
                print(f"[mcp] 工具名冲突，先注册者胜出，跳过: {full}")
                continue
            risk = risk_map.get(full, risk_map.get(raw_name))
            if risk is not None and risk not in LEVELS:
                print(f"[mcp] 工具 {full} 的风险配置非法（{risk}），回落默认 {DEFAULT_RISK}")
                risk = None
            params = tool.get("inputSchema")
            if not isinstance(params, dict):
                params = {"type": "object", "properties": {}}
            self.entries[full] = {
                "server": server_name,
                "tool": raw_name,
                "description": str(tool.get("description") or ""),
                "parameters": params,
                "risk": risk or DEFAULT_RISK,
            }
            count += 1
        return count

    def _start_server(self, cfg: dict, reserved_names) -> None:
        """连一个 Server：起进程 → 握手 → tools/list → 注册。
        失败抛给 start() 统一记日志跳过，这里不吞。"""
        name = str(cfg.get("name") or "")
        command = cfg.get("command")
        args = cfg.get("args") or []
        if not name or not isinstance(command, list) or not command:
            raise MCPError(f"配置缺 name 或 command: {cfg!r}")
        full_command = [str(c) for c in command] + [str(a) for a in args]
        env = cfg.get("env") if isinstance(cfg.get("env"), dict) else {}
        client = MCPClient(name, full_command, env)
        try:
            client.start()
            tools = client.list_tools()
        except Exception:
            client.close()
            raise
        # 内置名先占位：靠 entries 冲突规则实现“内置胜出”。
        for reserved in reserved_names:
            self.entries.setdefault(reserved, {"builtin": True})
        allow = cfg.get("allow") if isinstance(cfg.get("allow"), list) else None
        risk_map = cfg.get("risk") if isinstance(cfg.get("risk"), dict) else {}
        registered = self.register_tools(name, tools, risk_map=risk_map, allow=allow)
        self.clients[name] = client
        print(f"[mcp] server {name} 已连接，注册 {registered} 个工具")

    def start(self, configs: list[dict], reserved_names=()) -> None:
        """按配置逐个连 Server；单个失败只跳过它自己，绝不外抛。"""
        for cfg in configs:
            if not isinstance(cfg, dict):
                print(f"[mcp] 跳过非法配置项: {cfg!r}")
                continue
            try:
                self._start_server(cfg, reserved_names)
            except Exception as e:  # 握手/进程/协议任何失败都只跳过这个 Server
                print(f"[mcp] server {cfg.get('name')} 接入失败，已跳过: {e}")
        # 占位用的内置名条目不是 MCP 工具，清掉别混进 schema/分级查询。
        for key in [k for k, v in self.entries.items() if v.get("builtin")]:
            del self.entries[key]

    def close_all(self) -> None:
        for client in self.clients.values():
            client.close()
        self.clients.clear()

    # ---------- 查询与调用（给 tools.py / approval.py 用） ----------

    def is_registered(self, full_name: str) -> bool:
        return full_name in self.entries

    def risk_for(self, full_name: str) -> str | None:
        entry = self.entries.get(full_name)
        return entry["risk"] if entry else None

    def schemas(self) -> list[dict]:
        """注册表里全部 MCP 工具的 OpenAI function schema 列表。"""
        out = []
        for full, entry in self.entries.items():
            description = entry["description"] or f"MCP 工具（server: {entry['server']}）"
            out.append({
                "type": "function",
                "function": {
                    "name": full,
                    "description": description,
                    "parameters": entry["parameters"],
                },
            })
        return out

    def call(self, full_name: str, arguments: dict) -> str:
        entry = self.entries.get(full_name)
        if entry is None:
            raise MCPError(f"未注册的 MCP 工具: {full_name}")
        client = self.clients.get(entry["server"])
        if client is None:
            raise MCPError(f"MCP server {entry['server']} 未连接")
        return client.call_tool(entry["tool"], arguments or {})


# 进程内单例：main.py 的 lifespan 启动时填充，关闭时清理。
REGISTRY = MCPRegistry()


def start_from_env(reserved_names=()) -> MCPRegistry:
    """读 .env 的 MCP_SERVERS_JSON 建注册表（lifespan 调用）。

    没配 / 配错都只记日志、返回空注册表——MCP 是加法，不是启动
    前置条件，没它应用照样只有 5 个内置工具。"""
    raw = (os.environ.get("MCP_SERVERS_JSON") or "").strip()
    if not raw:
        print("[mcp] 未配置 MCP_SERVERS_JSON，只有内置工具")
        return REGISTRY
    try:
        configs = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"[mcp] MCP_SERVERS_JSON 不是合法 JSON，已忽略: {e}")
        return REGISTRY
    if not isinstance(configs, list):
        print("[mcp] MCP_SERVERS_JSON 顶层应为数组，已忽略")
        return REGISTRY
    REGISTRY.start(configs, reserved_names)
    print(f"[mcp] 注册表就绪：{len(REGISTRY.entries)} 个 MCP 工具"
          f"（server: {', '.join(REGISTRY.clients) or '无'}）")
    return REGISTRY


def shutdown() -> None:
    """应用关闭时关掉全部 Server 子进程（lifespan 调用）。"""
    REGISTRY.close_all()


# ---- 给 tools.py / approval.py 用的薄封装（都查单例 REGISTRY） ----

def is_registered(full_name: str) -> bool:
    return REGISTRY.is_registered(full_name)


def registered_risk(full_name: str) -> str | None:
    return REGISTRY.risk_for(full_name)


def registered_schemas() -> list[dict]:
    return REGISTRY.schemas()


def call_tool(full_name: str, arguments: dict) -> str:
    return REGISTRY.call(full_name, arguments)
