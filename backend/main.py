"""后端编排层：FastAPI + Agent Loop（调模型 → 执行工具 → 回喂 → 循环）。"""
import asyncio
import json
import os
from contextlib import asynccontextmanager

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .approval import (
    HIGH,
    LOW,
    MEDIUM,
    ApprovalStore,
    classify,
    log_audit,
    wait_for_decision,
)
from .context import apply_budget
from .memory import memory_system_message
from .storage import (
    DEFAULT_HISTORY_ROUNDS,
    ensure_session,
    list_sessions,
    load_history,
    save_message,
)
from . import mcp
from .tools import TOOL_SCHEMAS, WORKSPACE, all_tool_schemas, run_tool
from .usage import (
    merge_call_totals,
    record_usage,
    summarize_usage,
    take_embedding_usage,
)

load_dotenv()
LLM_API_KEY = os.environ.get("LLM_API_KEY", "")
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "https://api.openai.com/v1").rstrip("/")
LLM_MODEL = os.environ.get("LLM_MODEL", "gpt-4o")
MAX_ROUNDS = int(os.environ.get("MAX_TOOL_ROUNDS", "12"))
ACCESS_TOKEN = os.environ.get("ACCESS_TOKEN", "")  # 设置后 /api/chat 需带 x-access-token

SYSTEM_PROMPT = """你是一个个人 AI Agent，运行在用户的服务器上。
你可以通过工具在工作区里读写文件、写代码、执行命令来完成任务。
需要把信息写入文件时，直接调用 write_file 工具完成写入，不要只在对话里给出拟写入内容；中风险写入会弹出审批卡，由用户确认，不用在对话里先征求同意。
做多步任务时：先看现状，再动手，跑完要验证结果，最后用简洁的话汇报。
不要编造工具结果；失败了就看报错、修正、重试。"""

@asynccontextmanager
async def lifespan(_app):
    """应用生命周期：启动时接 MCP Server、关停时清理子进程（v0.13）。

    MCP 是加法不是启动前置：没配 MCP_SERVERS_JSON、配置写错、某个
    Server 握手失败，都只在 mcp.start_from_env 里记日志跳过，这里
    不会抛。握手是阻塞 IO，放 to_thread 里别卡事件循环。注册到的
    工具经 tools.all_tool_schemas() 与内置 5 工具合并后发给模型。"""
    await asyncio.to_thread(
        mcp.start_from_env,
        {schema["function"]["name"] for schema in TOOL_SCHEMAS},
    )
    yield
    mcp.shutdown()


app = FastAPI(lifespan=lifespan)

# 待确认的审批卡：内存 dict（v0.5），重启就丢，未确认不执行。
approvals = ApprovalStore()


def _denied_text(tool_name: str, level: str, reason: str, stream: bool) -> str:
    """工具调用没执行时回喂给模型的失败文本——复用 v0.1 就有的
    “失败文本回喂自纠”，不抛异常、不 500、不打断循环，模型自己
    改方案重试。"""
    if level == HIGH:
        return f"工具执行失败: {tool_name} 调用被拒绝（高风险：{reason}）"
    if stream:
        return f"工具执行失败: 用户拒绝了本次 {tool_name} 调用（{reason}）"
    return (f"工具执行失败: {tool_name} 调用需要人工确认（{reason}）；"
            "请在流式对话（/api/chat/stream）里审批通过后重试")


# ---------------- v0.12：长期记忆写入护栏（只挂流式循环） ----------------
# t09 四轮 live 样本（败/过/过/败）证明：模型读完 MEMORY.md、用户也
# 明说要写入时，仍可能以纯文本收尾，write_file 根本没发出——丢的不是
# “知不知道”，是“读完→收尾”这一步的工具调用。v0.9 往 SYSTEM_PROMPT
# 加的那句只是把发生率压低、没根治；本版改在循环层兜：三条件齐备且
# 本请求还没推过时，往本轮上下文塞一条提醒、续跑一轮，逼模型要么真
# 发出 write_file（仍走中风险审批卡），要么明说不写。系统绝不代写：
# 代写 = 绕过审批卡，是红线。非流式 /api/chat 按设计本就拒绝写
# MEMORY.md（等不起人点卡），在那里推只会把模型推向注定被拒的调用，
# 所以护栏只做流式，非流式保持现状（已知边界，见 v0.12 设计草稿）。
MEMORY_NUDGE_TEXT = (
    "系统提醒：你本轮已读取 MEMORY.md，用户明确要求把内容写入长期记忆，"
    "但你尚未发出 write_file 调用。请要么现在发出 write_file 把内容写进 "
    "MEMORY.md（写入会弹出审批卡由用户确认），要么在回复中明确说明你决定"
    "不写以及原因。不要只给出拟写入的内容就结束。"
)
# 写入类动词与记忆类名词须同时出现才算“明确要求写长期记忆”。
# 判定故意保守：只读提问（“我的偏好是什么”）不含写入动词，天然出局；
# 宁可漏推，也不能把正常答疑误推成写入。
_MEMORY_WRITE_VERBS = ("写进", "写入", "记进", "记到", "记下", "存进", "存到", "保存到")
_MEMORY_NOUNS = ("MEMORY", "长期记忆")


def _memory_write_intent(user_message: str) -> bool:
    """用户消息是否明确要求写入长期记忆（护栏条件 ①）。"""
    text = user_message or ""
    upper = text.upper()
    return (any(v in text for v in _MEMORY_WRITE_VERBS)
            and any(n in upper for n in _MEMORY_NOUNS))


def _is_memory_path(args: dict | None) -> bool:
    """工具参数 path 是否就是工作区根的 MEMORY.md（与 approval 分级
    口径一致：只认根目录那个长期记忆文件，子目录同名文件不算）。"""
    rel = ((args or {}).get("path") or "").replace("\\", "/")
    while rel.startswith("./"):
        rel = rel[2:]
    return rel == "MEMORY.md"


def _memory_nudge_needed(user_message: str,
                         request_calls: list[tuple[str, dict]]) -> bool:
    """护栏触发判定——同一请求内三条件全满足才返回 True：
    ① 用户明确要求写入长期记忆（_memory_write_intent）；
    ② 本请求读过 MEMORY.md（read_file 且 path 为 MEMORY.md）；
    ③ 本请求没发出过对 MEMORY.md 的 write_file（发过即算发过，
    成败不论——审批被拒是审批的事，护栏不重复推）。"""
    if not _memory_write_intent(user_message):
        return False
    if not any(name == "read_file" and _is_memory_path(args)
               for name, args in request_calls):
        return False
    return not any(name == "write_file" and _is_memory_path(args)
                   for name, args in request_calls)


class ChatIn(BaseModel):
    message: str
    history: list[dict] = []  # [{"role": "user"/"assistant", "content": "..."}]
    session_id: str | None = None  # 带上则走服务端会话（优先用库里的历史）


def _resolve_context(body: ChatIn, session_id: str) -> list[dict]:
    """组装本轮上下文：带 session_id 时以服务端库里的最近 N 轮为准，
    否则用前端传来的 history（老用法兼容）。"""
    if body.session_id:
        return load_history(session_id, rounds=DEFAULT_HISTORY_ROUNDS)
    return body.history


def _build_messages_with_budget(body: ChatIn, session_id: str) -> tuple[list[dict], dict]:
    """组装发给模型的完整上下文：[system, memory?, *history, user]。

    memory 是第二条 system 消息（v0.4 长期记忆，有 MEMORY.md 才有）；
    history 按 _resolve_context 来。组装完过一遍 v0.6 预算截断
    （context.apply_budget），返回 (messages, {"context_tokens": 估算值,
    "dropped_rounds": 本轮丢掉的旧轮数})。/api/chat 与 stream_agent 共用，
    避免两处组装口径漂移。截断只发生在组装时，storage.py 一行不改。
    """
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    memory = memory_system_message()
    if memory:
        messages.append(memory)
    messages.extend(_resolve_context(body, session_id))
    messages.append({"role": "user", "content": body.message})
    messages, stats = apply_budget(messages)
    if stats["dropped_rounds"]:
        print(f"[context] 超预算，丢弃 {stats['dropped_rounds']} 轮旧历史"
              f"（context_tokens={stats['context_tokens']}）")
    return messages, stats


def _build_messages(body: ChatIn, session_id: str) -> list[dict]:
    """只取 messages 部分（不带预算统计），供只需要上下文的地方用。"""
    messages, _ = _build_messages_with_budget(body, session_id)
    return messages


# 最近一次模型响应的 usage 字段（v0.6 开始记录，供第三段成本统计用；
# 单人使用，模块级变量够用）。响应没带 usage 时为 None。
LAST_LLM_USAGE: dict | None = None


async def call_llm_with_usage(messages: list[dict]) -> tuple[dict, dict | None]:
    """调一次模型，返回 (message, usage)。usage 是响应里的 usage 字段
    （prompt_tokens 等），没有就返回 None——调用方用估算值兜底。"""
    global LAST_LLM_USAGE
    async with httpx.AsyncClient(timeout=120) as client:
        r = await client.post(
            f"{LLM_BASE_URL}/chat/completions",
            headers={"Authorization": f"Bearer {LLM_API_KEY}"},
            json={"model": LLM_MODEL, "messages": messages, "tools": all_tool_schemas()},
        )
        r.raise_for_status()
        data = r.json()
        LAST_LLM_USAGE = data.get("usage") or None
        return data["choices"][0]["message"], LAST_LLM_USAGE


def _log_llm_usage(context_tokens: int) -> None:
    """记一行真实用量日志：响应带 usage 就记 prompt_tokens，没带就用
    估算的 context_tokens 兜底。真实用量留给第三段的成本统计用，
    这里只记、不统计。"""
    usage = LAST_LLM_USAGE or {}
    prompt_tokens = usage.get("prompt_tokens")
    if prompt_tokens is not None:
        print(f"[context] 真实 prompt_tokens={prompt_tokens}"
              f"（估算 context_tokens={context_tokens}）")
    else:
        print(f"[context] 响应未带 usage，prompt_tokens 用估算值 {context_tokens}")


async def call_llm(messages: list[dict]) -> dict:
    msg, _ = await call_llm_with_usage(messages)
    return msg


@app.get("/health")
async def health():
    return {"ok": True, "model": LLM_MODEL, "llm_key_configured": bool(LLM_API_KEY)}


@app.post("/api/chat")
async def chat(body: ChatIn, x_access_token: str = Header(default="")):
    if ACCESS_TOKEN and x_access_token != ACCESS_TOKEN:
        raise HTTPException(status_code=401, detail="ACCESS_TOKEN 不正确或缺失")
    if not LLM_API_KEY:
        raise HTTPException(status_code=500, detail="服务器未配置 LLM_API_KEY，请在 .env 中填写")
    # 会话：没带 session_id 就新建一个（不污染已有会话）。
    # 顺序注意：先按库里历史组装上下文，再把本轮 user 落库——
    # 反过来会把本轮 user 在上下文里存两份。
    session_id = ensure_session(body.session_id, title_hint=body.message)
    # v0.11：先取走并丢弃该 session 残留的 embedding 暂存（上一个没
    # 正常收尾的请求留下的），防跨请求串账；本请求的在返回时取。
    take_embedding_usage(session_id)
    messages, ctx = _build_messages_with_budget(body, session_id)
    save_message(session_id, "user", body.message)
    tool_log = []
    usage_rows = []  # v0.10：本请求内每次 LLM 调用的真实用量，逐次落盘
    for _ in range(MAX_ROUNDS):
        try:
            msg, usage = await call_llm_with_usage(messages)
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"调用模型 API 失败，请检查 LLM_BASE_URL / LLM_API_KEY / LLM_MODEL: {e}") from e
        usage_rows.append(record_usage(session_id=session_id, model=LLM_MODEL,
                                       stream=False, usage=usage,
                                       messages=messages))
        messages.append(msg)
        tool_calls = msg.get("tool_calls") or []
        if not tool_calls:  # 没有工具调用 = 最终回复
            reply = msg.get("content") or ""
            save_message(session_id, "assistant", reply)
            _log_llm_usage(ctx["context_tokens"])
            return {"reply": reply, "tool_log": tool_log, "session_id": session_id,
                    "context_tokens": ctx["context_tokens"],
                    "dropped_rounds": ctx["dropped_rounds"],
                    "usage": merge_call_totals(usage_rows),
                    "embedding_usage": take_embedding_usage(session_id)}
        for call in tool_calls:
            name = call["function"]["name"]
            args = json.loads(call["function"]["arguments"] or "{}")
            # v0.5：非流式一次 HTTP 请求走完整个循环，等不起人点卡，
            # 所以中 / 高风险调用直接按拒绝回喂（并说明流式下可审批）。
            level, reason = classify(name, args, WORKSPACE)
            if level == LOW:
                result = run_tool(name, args, session_id=session_id)
                log_audit(session_id=session_id, tool=name, args=args, level=level,
                          outcome="auto", result_summary=result)
            else:
                result = _denied_text(name, level, reason, stream=False)
                log_audit(session_id=session_id, tool=name, args=args, level=level,
                          outcome="high_denied" if level == HIGH else "needs_approval",
                          result_summary=result)
            tool_log.append({"tool": name, "args": args, "result": result[:500]})
            messages.append({"role": "tool", "tool_call_id": call["id"], "content": result})
    reply = "已达到最大工具轮数，任务暂停，请检查进度。"
    save_message(session_id, "assistant", reply)
    _log_llm_usage(ctx["context_tokens"])
    return {"reply": reply, "tool_log": tool_log, "session_id": session_id,
            "context_tokens": ctx["context_tokens"],
            "dropped_rounds": ctx["dropped_rounds"],
            "usage": merge_call_totals(usage_rows),
            "embedding_usage": take_embedding_usage(session_id)}


def _sse(payload: dict) -> str:
    """把一个事件对象编码成一条 SSE 消息。"""
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


async def stream_agent(body: ChatIn, session_id: str):
    """Agent 循环的流式版：token 增量实时发，工具调用先攒齐再执行。

    关键点：流式返回里 tool_calls 是按 index 拆成多个 delta 到达的，
    必须先把同一 index 的 id / name / arguments 拼完整，才能执行；
    边收边执行会拿到残缺参数。reasoning_content（思考过程）不在
    content 字段里，忽略即可，不影响正文流。

    持久化：user 在组装完上下文后、进循环前落库；assistant 最终回复在
    done 时落库，中途失败不写 assistant，避免半截回复污染下一轮。
    """
    # v0.11：同 /api/chat，先清掉该 session 残留的 embedding 暂存防串账。
    take_embedding_usage(session_id)
    yield _sse({"type": "session", "session_id": session_id})
    messages, ctx = _build_messages_with_budget(body, session_id)
    # 顺序注意：先组装上下文再落库，否则本轮 user 会在上下文里出现两次。
    save_message(session_id, "user", body.message)
    usage_rows = []  # v0.10：本请求内每次 LLM 调用的真实用量，逐次落盘
    # v0.12 护栏状态：本请求已发出的工具调用（判定“读过/写过 MEMORY.md”
    # 用）与“已推过一次”标记。护栏每请求最多推一次，MAX_ROUNDS 不变。
    request_calls: list[tuple[str, dict]] = []
    nudged = False
    for round_no in range(MAX_ROUNDS):
        content_parts: list[str] = []
        calls: dict[int, dict] = {}
        round_usage: dict | None = None
        try:
            async with httpx.AsyncClient(timeout=120) as client:
                async with client.stream(
                    "POST",
                    f"{LLM_BASE_URL}/chat/completions",
                    headers={"Authorization": f"Bearer {LLM_API_KEY}"},
                    json={"model": LLM_MODEL, "messages": messages, "tools": all_tool_schemas(),
                          "stream": True,
                          # v0.10：流式 usage 只在带这个开关时于尾 chunk 返回。
                          "stream_options": {"include_usage": True}},
                ) as resp:
                    resp.raise_for_status()
                    async for line in resp.aiter_lines():
                        if not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if data == "[DONE]":
                            break
                        chunk = json.loads(data)
                        # usage 可能挂在最后一个 delta chunk 上，也可能在
                        # choices 为空的独立尾 chunk 上，两种都收；先判空
                        # 再取 choices[0]，否则尾 chunk 会 IndexError。
                        if chunk.get("usage"):
                            round_usage = chunk["usage"]
                        choices = chunk.get("choices") or []
                        if not choices:
                            continue
                        delta = choices[0].get("delta") or {}
                        if delta.get("content"):
                            content_parts.append(delta["content"])
                            yield _sse({"type": "token", "text": delta["content"]})
                        for tc in delta.get("tool_calls") or []:
                            idx = tc["index"]
                            slot = calls.setdefault(idx, {"id": "", "name": "", "arguments": ""})
                            if tc.get("id"):
                                slot["id"] = tc["id"]
                            fn = tc.get("function") or {}
                            if fn.get("name"):
                                slot["name"] = fn["name"]
                            if fn.get("arguments"):
                                slot["arguments"] += fn["arguments"]
        except Exception as e:
            yield _sse({"type": "error", "detail": f"调用模型 API 失败，请检查 LLM_BASE_URL / LLM_API_KEY / LLM_MODEL: {e}"})
            return
        # 本轮流结束才记账：messages 此刻还是本轮实际发送的内容，
        # 流式没等到 usage 时 record_usage 用它估算并标 estimated。
        usage_rows.append(record_usage(session_id=session_id, model=LLM_MODEL,
                                       stream=True, usage=round_usage,
                                       messages=messages))
        tool_calls = [
            {"id": c["id"], "type": "function",
             "function": {"name": c["name"], "arguments": c["arguments"]}}
            for _, c in sorted(calls.items())
        ]
        messages.append({"role": "assistant",
                         "content": "".join(content_parts) or None,
                         "tool_calls": tool_calls or None})
        if not tool_calls:
            # v0.12 护栏：没发工具调用本该收尾，但若三条件齐备（用户明说
            # 要写长期记忆 + 本请求读过 MEMORY.md + 没发过 write_file）
            # 且还没推过、且后面还有轮次可用，就不收尾——往本轮上下文
            # 塞一条提醒续跑一轮。提醒只进上下文、不落会话库；第二次再
            # 以纯文本收尾就如实返回，系统不代写（红线）。
            if (not nudged and round_no + 1 < MAX_ROUNDS
                    and _memory_nudge_needed(body.message, request_calls)):
                nudged = True
                log_audit(session_id=session_id, tool="memory_guardrail",
                          args={"path": "MEMORY.md"}, level=LOW,
                          outcome="memory_write_nudge",
                          result_summary="用户要求写长期记忆且已读 MEMORY.md，"
                                         "模型以纯文本收尾未发 write_file；"
                                         "注入提醒续跑一轮")
                messages.append({"role": "user", "content": MEMORY_NUDGE_TEXT})
                continue
            reply = "".join(content_parts)
            save_message(session_id, "assistant", reply)
            yield _sse({"type": "done", "reply": reply, "session_id": session_id,
                        "context_tokens": ctx["context_tokens"],
                        "dropped_rounds": ctx["dropped_rounds"],
                        "usage": merge_call_totals(usage_rows),
                        "embedding_usage": take_embedding_usage(session_id)})
            return
        for call in tool_calls:
            name = call["function"]["name"]
            args = json.loads(call["function"]["arguments"] or "{}")
            request_calls.append((name, args))  # v0.12：护栏判定用（发过即算）
            level, reason = classify(name, args, WORKSPACE)
            if level == HIGH:
                # 高风险：默认拒绝，卡上也不给过，直接回喂失败文本。
                result = _denied_text(name, level, reason, stream=True)
                log_audit(session_id=session_id, tool=name, args=args, level=level,
                          outcome="high_denied", result_summary=result)
            elif level == MEDIUM:
                # 中风险：先停住，发审批卡事件等用户点卡；5 分钟无人确认
                # 按拒绝处理。点通过只放行这一次调用，不做“以后都允许”。
                approval_id = approvals.create(tool=name, args=args, level=level,
                                               reason=reason, session_id=session_id)
                yield _sse({"type": "approval_required", "approval_id": approval_id,
                            "tool": name, "args": args, "level": level, "reason": reason})
                decision = await wait_for_decision(approvals, approval_id)
                if decision == "approve":
                    yield _sse({"type": "tool", "tool": name, "args": args})
                    result = run_tool(name, args, approved=True,
                                      session_id=session_id)
                    log_audit(session_id=session_id, tool=name, args=args, level=level,
                              outcome="approved", result_summary=result)
                else:
                    result = _denied_text(name, level,
                                          reason + ("（5 分钟未确认，按拒绝处理）" if decision == "timeout" else ""),
                                          stream=True)
                    log_audit(session_id=session_id, tool=name, args=args, level=level,
                              outcome="timeout" if decision == "timeout" else "denied",
                              result_summary=result)
            else:
                yield _sse({"type": "tool", "tool": name, "args": args})
                result = run_tool(name, args, session_id=session_id)
                log_audit(session_id=session_id, tool=name, args=args, level=level,
                          outcome="auto", result_summary=result)
            yield _sse({"type": "tool_result", "tool": name, "result": result[:500]})
            messages.append({"role": "tool", "tool_call_id": call["id"], "content": result})
    reply = "已达到最大工具轮数，任务暂停，请检查进度。"
    save_message(session_id, "assistant", reply)
    yield _sse({"type": "done", "reply": reply, "session_id": session_id,
                "context_tokens": ctx["context_tokens"],
                "dropped_rounds": ctx["dropped_rounds"],
                "usage": merge_call_totals(usage_rows),
                "embedding_usage": take_embedding_usage(session_id)})


@app.post("/api/chat/stream")
async def chat_stream(body: ChatIn, x_access_token: str = Header(default="")):
    if ACCESS_TOKEN and x_access_token != ACCESS_TOKEN:
        raise HTTPException(status_code=401, detail="ACCESS_TOKEN 不正确或缺失")
    if not LLM_API_KEY:
        raise HTTPException(status_code=500, detail="服务器未配置 LLM_API_KEY，请在 .env 中填写")
    # 会话：没带 session_id 就新建一个（不污染已有会话）。
    # user 的落库在 stream_agent 里、组装完上下文后做（见上）。
    session_id = ensure_session(body.session_id, title_hint=body.message)
    return StreamingResponse(stream_agent(body, session_id), media_type="text/event-stream")


class ApprovalIn(BaseModel):
    decision: str  # "approve" | "deny"


@app.post("/api/approvals/{approval_id}")
async def decide_approval(approval_id: str, body: ApprovalIn,
                           x_access_token: str = Header(default="")):
    """前端审批卡的两个按钮调这个接口：通过 / 拒绝一次中风险调用。

    审批只确认“这一次调用”，不做“以后都允许”。卡不存在或已过期
    （5 分钟未确认按拒绝处理）返回 404，流式循环那边已经按拒绝走了。"""
    if ACCESS_TOKEN and x_access_token != ACCESS_TOKEN:
        raise HTTPException(status_code=401, detail="ACCESS_TOKEN 不正确或缺失")
    if body.decision not in ("approve", "deny"):
        raise HTTPException(status_code=400, detail="decision 只能是 approve 或 deny")
    rec = approvals.decide(approval_id, body.decision)
    if rec is None:
        raise HTTPException(status_code=404, detail="审批不存在或已过期（5 分钟未确认按拒绝处理）")
    return {"approval_id": approval_id, "decision": body.decision, "tool": rec["tool"]}


@app.get("/api/usage")
async def usage_summary(x_access_token: str = Header(default="")):
    """成本统计出口：data/usage.log 的聚合（总计 + 按日 + 按会话 +
    按 kind 分账）。v0.11 起总计含 embedding，总账不再低估；只看聊天
    或只看 embedding 读 by_kind。

    只读 JSON 直出，不做前端页面；api / estimated 分开报，
    见 backend/usage.py 的口径说明。"""
    if ACCESS_TOKEN and x_access_token != ACCESS_TOKEN:
        raise HTTPException(status_code=401, detail="ACCESS_TOKEN 不正确或缺失")
    return {"usage": summarize_usage()}


@app.get("/api/sessions")
async def sessions_list(x_access_token: str = Header(default="")):
    if ACCESS_TOKEN and x_access_token != ACCESS_TOKEN:
        raise HTTPException(status_code=401, detail="ACCESS_TOKEN 不正确或缺失")
    return {"sessions": list_sessions()}


@app.get("/api/sessions/{session_id}/messages")
async def session_messages(session_id: str, x_access_token: str = Header(default="")):
    if ACCESS_TOKEN and x_access_token != ACCESS_TOKEN:
        raise HTTPException(status_code=401, detail="ACCESS_TOKEN 不正确或缺失")
    history = load_history(session_id, rounds=DEFAULT_HISTORY_ROUNDS)
    if not history:
        raise HTTPException(status_code=404, detail="会话不存在或没有消息")
    return {"session_id": session_id, "messages": history}


app.mount("/", StaticFiles(directory="static", html=True), name="static")
