"""后端编排层：FastAPI + Agent Loop（调模型 → 执行工具 → 回喂 → 循环）。"""
import json
import os

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .tools import TOOL_SCHEMAS, run_tool

load_dotenv()
LLM_API_KEY = os.environ.get("LLM_API_KEY", "")
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "https://api.openai.com/v1").rstrip("/")
LLM_MODEL = os.environ.get("LLM_MODEL", "gpt-4o")
MAX_ROUNDS = int(os.environ.get("MAX_TOOL_ROUNDS", "12"))
ACCESS_TOKEN = os.environ.get("ACCESS_TOKEN", "")  # 设置后 /api/chat 需带 x-access-token

SYSTEM_PROMPT = """你是一个个人 AI Agent，运行在用户的服务器上。
你可以通过工具在工作区里读写文件、写代码、执行命令来完成任务。
做多步任务时：先看现状，再动手，跑完要验证结果，最后用简洁的话汇报。
不要编造工具结果；失败了就看报错、修正、重试。"""

app = FastAPI()


class ChatIn(BaseModel):
    message: str
    history: list[dict] = []  # [{"role": "user"/"assistant", "content": "..."}]


async def call_llm(messages: list[dict]) -> dict:
    async with httpx.AsyncClient(timeout=120) as client:
        r = await client.post(
            f"{LLM_BASE_URL}/chat/completions",
            headers={"Authorization": f"Bearer {LLM_API_KEY}"},
            json={"model": LLM_MODEL, "messages": messages, "tools": TOOL_SCHEMAS},
        )
        r.raise_for_status()
        return r.json()["choices"][0]["message"]


@app.get("/health")
async def health():
    return {"ok": True, "model": LLM_MODEL, "llm_key_configured": bool(LLM_API_KEY)}


@app.post("/api/chat")
async def chat(body: ChatIn, x_access_token: str = Header(default="")):
    if ACCESS_TOKEN and x_access_token != ACCESS_TOKEN:
        raise HTTPException(status_code=401, detail="ACCESS_TOKEN 不正确或缺失")
    if not LLM_API_KEY:
        raise HTTPException(status_code=500, detail="服务器未配置 LLM_API_KEY，请在 .env 中填写")
    messages = [{"role": "system", "content": SYSTEM_PROMPT}, *body.history,
                {"role": "user", "content": body.message}]
    tool_log = []
    for _ in range(MAX_ROUNDS):
        try:
            msg = await call_llm(messages)
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"调用模型 API 失败，请检查 LLM_BASE_URL / LLM_API_KEY / LLM_MODEL: {e}") from e
        messages.append(msg)
        tool_calls = msg.get("tool_calls") or []
        if not tool_calls:  # 没有工具调用 = 最终回复
            return {"reply": msg.get("content") or "", "tool_log": tool_log}
        for call in tool_calls:
            args = json.loads(call["function"]["arguments"] or "{}")
            result = run_tool(call["function"]["name"], args)
            tool_log.append({"tool": call["function"]["name"], "args": args, "result": result[:500]})
            messages.append({"role": "tool", "tool_call_id": call["id"], "content": result})
    return {"reply": "已达到最大工具轮数，任务暂停，请检查进度。", "tool_log": tool_log}


def _sse(payload: dict) -> str:
    """把一个事件对象编码成一条 SSE 消息。"""
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


async def stream_agent(body: ChatIn):
    """Agent 循环的流式版：token 增量实时发，工具调用先攒齐再执行。

    关键点：流式返回里 tool_calls 是按 index 拆成多个 delta 到达的，
    必须先把同一 index 的 id / name / arguments 拼完整，才能执行；
    边收边执行会拿到残缺参数。reasoning_content（思考过程）不在
    content 字段里，忽略即可，不影响正文流。
    """
    messages = [{"role": "system", "content": SYSTEM_PROMPT}, *body.history,
                {"role": "user", "content": body.message}]
    for _ in range(MAX_ROUNDS):
        content_parts: list[str] = []
        calls: dict[int, dict] = {}
        try:
            async with httpx.AsyncClient(timeout=120) as client:
                async with client.stream(
                    "POST",
                    f"{LLM_BASE_URL}/chat/completions",
                    headers={"Authorization": f"Bearer {LLM_API_KEY}"},
                    json={"model": LLM_MODEL, "messages": messages, "tools": TOOL_SCHEMAS, "stream": True},
                ) as resp:
                    resp.raise_for_status()
                    async for line in resp.aiter_lines():
                        if not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if data == "[DONE]":
                            break
                        delta = json.loads(data)["choices"][0]["delta"]
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
        tool_calls = [
            {"id": c["id"], "type": "function",
             "function": {"name": c["name"], "arguments": c["arguments"]}}
            for _, c in sorted(calls.items())
        ]
        messages.append({"role": "assistant",
                         "content": "".join(content_parts) or None,
                         "tool_calls": tool_calls or None})
        if not tool_calls:
            yield _sse({"type": "done", "reply": "".join(content_parts)})
            return
        for call in tool_calls:
            args = json.loads(call["function"]["arguments"] or "{}")
            yield _sse({"type": "tool", "tool": call["function"]["name"], "args": args})
            result = run_tool(call["function"]["name"], args)
            yield _sse({"type": "tool_result", "tool": call["function"]["name"],
                        "result": result[:500]})
            messages.append({"role": "tool", "tool_call_id": call["id"], "content": result})
    yield _sse({"type": "done", "reply": "已达到最大工具轮数，任务暂停，请检查进度。"})


@app.post("/api/chat/stream")
async def chat_stream(body: ChatIn, x_access_token: str = Header(default="")):
    if ACCESS_TOKEN and x_access_token != ACCESS_TOKEN:
        raise HTTPException(status_code=401, detail="ACCESS_TOKEN 不正确或缺失")
    if not LLM_API_KEY:
        raise HTTPException(status_code=500, detail="服务器未配置 LLM_API_KEY，请在 .env 中填写")
    return StreamingResponse(stream_agent(body), media_type="text/event-stream")


app.mount("/", StaticFiles(directory="static", html=True), name="static")
