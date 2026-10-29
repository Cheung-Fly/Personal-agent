"""工具调用审批分级（v0.5）：只读自动、改动确认、高危拒绝。

设计口径（见设计草稿 v0.5-审批分级设计草稿.md）：
- 三级：low 自动执行、medium 弹卡确认一次、high 默认拒绝（卡上也不给过）。
- 判定只看第一段 token + 危险 flag 列表，不做完整 shell 解析；
  解析不出的、含 && / ; / | 串联的，一律升一级——宁可多弹一次卡，
  不放过一条可疑命令。
- Chung 2026-10-27 定：跑测试自动（pytest 属低风险），真正写盘 /
  改代码的命令弹卡；MEMORY.md 自动写入口这次开——Agent 提议写
  记忆，永远走中风险审批卡，点了通过才写。
- 风险判定是启发式，不是形式化验证：模型换种写法绕过黑名单仍
  有可能，真正的兜底是 Docker 容器 + 工作区沙箱，审批只是多一层。
- 待确认状态放内存 dict（重启就丢，未确认的调用不执行，安全默认
  是拒绝）；审计日志写 data/audit.log JSON Lines，不进 git。

标准库，零新增依赖。
"""
import asyncio
import json
import os
import shlex
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

LOW = "low"  # 自动执行
MEDIUM = "medium"  # 弹卡确认一次
HIGH = "high"  # 默认拒绝

APPROVAL_TIMEOUT_SECONDS = int(os.environ.get("APPROVAL_TIMEOUT_SECONDS", "300"))

# 只读命令最小集：ls / cat / pwd 是纯读；python --version 只打印版本；
# pytest 跑测试（Chung 定：测试自动跑）；git status / git diff 只看不改。
_READ_ONLY_FIRST = {"ls", "cat", "pwd"}
# 写盘 / 改代码类命令弹卡；其余认不出的 exec 命令默认也弹卡（medium）。
# 高危只列设计草稿点名的几类：rm、sudo、chmod 777、下载后直接执行。
_HIGH_FIRST = {"rm", "sudo", "dd", "mkfs", "shutdown", "reboot"}
_EXEC_SHELLS = {"sh", "bash", "zsh", "dash"}
_DOWNLOADERS = {"curl", "wget"}
# write_file 写到这些名字 = 系统敏感路径，默认拒绝（MEMORY.md 除外，
# 它走 medium 审批卡；其余路径穿越本就被 tools._safe_path 拦住）。
_SENSITIVE_NAMES = {".env"}
_SENSITIVE_SUFFIXES = (".pem", ".key")


def _escalate(level: str) -> str:
    """升一级：low -> medium，medium/high -> high。"""
    if level == LOW:
        return MEDIUM
    return HIGH


def _has_chain(command: str) -> bool:
    """含串联算子（&& / || / ; / |）即算串联。粗糙但够用：
    误伤（引号里的分号）只会多弹一次卡，不会放过可疑命令。"""
    return any(op in command for op in ("&&", "||", ";", "|"))


def _classify_exec(command: str) -> tuple[str, str]:
    """判一条 shell 命令的风险级。返回 (level, reason)。"""
    if not command or not command.strip():
        return HIGH, "空命令"
    try:
        tokens = shlex.split(command)
    except ValueError:
        return HIGH, "命令无法解析"
    if not tokens:
        return HIGH, "空命令"
    chained = _has_chain(command)
    first = tokens[0]
    base = first.rsplit("/", 1)[-1]  # /bin/rm 也算 rm

    # 高危：点名的危险操作（先判，不受串联影响，本身已是最高级）。
    if base in _HIGH_FIRST:
        return HIGH, f"高危命令：{base}"
    if base == "chmod" and any(t in ("777", "a+rwx", "ugo+rwx") for t in tokens[1:]):
        return HIGH, "chmod 777 会放开全部权限"
    if base in _DOWNLOADERS and any(t in _EXEC_SHELLS for t in tokens):
        # curl ... | sh / wget ... | bash：对外下载后直接执行
        return HIGH, "下载后直接执行脚本"
    if base in _EXEC_SHELLS and "-c" in tokens:
        # sh -c '...'：内容不解析，直接按高危处理太严——按 medium 再看串联。
        level, reason = MEDIUM, "sh -c 执行拼接命令，需确认"
        return (_escalate(level), reason + "；含串联命令，升一级") if chained else (level, reason)

    # 低风险：只读最小集。
    if base in _READ_ONLY_FIRST:
        level, reason = LOW, f"只读命令：{base}"
    elif base in ("pytest", "python", "python3") and (
        base == "pytest" or (len(tokens) > 1 and tokens[1] == "-m" and len(tokens) > 2 and tokens[2] == "pytest")
        or (base in ("python", "python3") and tokens[1:] == ["--version"])
    ):
        level, reason = LOW, "跑测试 / 查版本，不改东西"
    elif base == "git" and len(tokens) > 1 and tokens[1] in ("status", "diff"):
        level, reason = LOW, "git 只读查看"
    # 中风险：执行代码、改代码 / 改状态的命令。
    elif base in ("python", "python3"):
        level, reason = MEDIUM, "执行代码，可能改文件"
    elif base == "git":
        level, reason = MEDIUM, "git 改状态操作，需确认"
    else:
        level, reason = MEDIUM, "非只读命令，需确认"

    if chained:
        return _escalate(level), reason + "；含串联命令，升一级"
    return level, reason


def classify(tool_name: str, args: dict, workspace: Path) -> tuple[str, str]:
    """判一次工具调用的风险级。返回 (level, reason)。

    - list_files / read_file / search_docs（v0.7 文档检索）：只读，低风险自动执行。
    - write_file：写新文件低风险；覆盖已有文件、写 MEMORY.md 中风险；
      写系统敏感路径（.env / .pem / .key / .ssh 下）高风险。
    - exec_command：见 _classify_exec。
    - 未知工具：高风险（反正也执行不了）。
    """
    if tool_name in ("list_files", "read_file", "search_docs"):
        return LOW, "只读，不改东西"
    if tool_name == "write_file":
        rel = args.get("path", "")
        try:
            p = (workspace / rel).resolve()
        except (OSError, ValueError):
            return HIGH, f"路径无法解析：{rel}"
        if p.name == "MEMORY.md" and p.parent == workspace.resolve():
            return MEDIUM, "写长期记忆 MEMORY.md，需人工确认"
        if p.name in _SENSITIVE_NAMES or p.suffix in _SENSITIVE_SUFFIXES or ".ssh" in p.parts:
            return HIGH, f"系统敏感路径：{rel}"
        if p.exists():
            return MEDIUM, f"覆盖已有文件：{rel}"
        return LOW, "写新文件"
    if tool_name == "exec_command":
        return _classify_exec(args.get("command", ""))
    return HIGH, f"未知工具：{tool_name}"


class ApprovalStore:
    """待确认的审批卡：内存 dict，重启就丢，未确认不执行。

    一条记录：{tool, args, level, reason, session_id, created_at,
    decision}，decision 为 None 表示还在等用户点卡。
    """

    def __init__(self, ttl_seconds: int = APPROVAL_TIMEOUT_SECONDS):
        self.ttl = ttl_seconds
        self._pending: dict[str, dict] = {}

    def create(self, *, tool: str, args: dict, level: str, reason: str, session_id: str) -> str:
        approval_id = str(uuid.uuid4())
        self._pending[approval_id] = {
            "tool": tool,
            "args": args,
            "level": level,
            "reason": reason,
            "session_id": session_id,
            "created_at": time.monotonic(),
            "decision": None,
        }
        return approval_id

    def get(self, approval_id: str) -> dict | None:
        rec = self._pending.get(approval_id)
        if rec is None:
            return None
        if time.monotonic() - rec["created_at"] > self.ttl:
            self._pending.pop(approval_id, None)
            return None
        return rec

    def decide(self, approval_id: str, decision: str) -> dict | None:
        """用户点卡：decision 为 "approve" / "deny"。
        不存在或已过期返回 None；否则记录决定并返回该记录。"""
        rec = self.get(approval_id)
        if rec is None:
            return None
        rec["decision"] = decision
        return rec

    def take(self, approval_id: str) -> dict | None:
        """取走一条记录（等到决定后调用，避免一张卡被重复消费）。"""
        return self._pending.pop(approval_id, None)


async def wait_for_decision(store: ApprovalStore, approval_id: str, poll_interval: float = 0.5) -> str:
    """等用户点卡。返回 "approve" / "deny" / "timeout"。

    5 分钟无人确认按拒绝处理；卡不存在（被清掉 / 重启丢了）也按拒绝——
    安全默认值永远是拒绝。注意“超时”和“卡没了”要分开：等着等着卡
    没了，只能是 TTL 到期被 get() 顺手清掉（取走只发生在决定之后），
    所以算 "timeout"；一开始卡就不在才算 "deny"。"""
    seen = False
    deadline = time.monotonic() + store.ttl
    while True:
        rec = store.get(approval_id)
        if rec is not None:
            seen = True
            if rec.get("decision") in ("approve", "deny"):
                store.take(approval_id)
                return rec["decision"]
        elif seen:
            return "timeout"
        if time.monotonic() > deadline:
            store.take(approval_id)
            return "timeout"
        await asyncio.sleep(poll_interval)


def audit_log_path() -> Path:
    """审计日志位置：data/audit.log（与 v0.3 的 SQLite 会话库分开，
    文件一眼能看；data/ 已进 .gitignore，不进 git）。"""
    return Path(os.environ.get("DATA_DIR", "./data")) / "audit.log"


def log_audit(*, session_id: str, tool: str, args: dict, level: str, outcome: str,
              result_summary: str = "") -> None:
    """每次工具调用记一行 JSON Lines：时间、会话、工具、参数摘要、
    风险级、通过/拒绝/自动、执行结果摘要。写失败不抛异常（审计不
    能拖垮主流程），只打一行日志。"""
    row = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "session_id": session_id,
        "tool": tool,
        "args": json.dumps(args, ensure_ascii=False)[:500],
        "level": level,
        "outcome": outcome,  # auto / approved / denied / timeout / high_denied / needs_approval
        "result": (result_summary or "")[:200],
    }
    try:
        p = audit_log_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError as e:
        print(f"[audit] 写审计日志失败: {e}")
