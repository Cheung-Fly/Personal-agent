"""确定性 mock LLM + mock embeddings（v0.8 评测专用，纯标准库）。

为什么要一个“会演戏”的假模型：评测集 mock 模式验的是评测链路
本身——任务定义、检查器、结果表、审批流、上下文截断、记忆注入
这些机制能不能跑通，不是验真模型有多强。所以这里的“大脑”是一组
确定性规则：看到什么上下文就回什么工具调用，可复现、零成本、
离线可跑。它只活在 mock 模式里；live 模式对着真模型跑，两个
成功率永远分开记（见 run_eval.py 与 eval/README.md）。

接口与 OpenAI-compatible 对齐，和 backend/main.py 的真实调用方式
一致：
- POST /v1/chat/completions：stream 与非 stream 都支持，流式下
  tool_calls 的 arguments 故意拆成两片发（同 v0.2 起的攒包考点）。
- POST /v1/embeddings：词袋计数假向量（与 tests/test_rag.py 同
  思路）——含同一关键词的文本方向一致、余弦高；完全无关的文本
  是零向量、余弦 0。按请求 dimensions 补零到指定维度。
"""
import itertools
import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# 假向量的词表：只统计这些词，其余词不贡献向量——无关问题（如
# “红烧肉的做法”）得到零向量、余弦 0，必然低于任何正阈值。
VOCAB = ["审批", "分级", "只读", "确认", "高危", "拒绝",
         "预算", "截断", "记忆", "文档", "会话", "部署"]


def fake_embed(texts, dimensions=1024):
    """词袋计数向量，补零/截断到 dimensions 维（补零不改变余弦）。"""
    out = []
    for t in texts:
        v = [float(t.count(w)) for w in VOCAB]
        if len(v) < dimensions:
            v += [0.0] * (dimensions - len(v))
        out.append(v[:dimensions])
    return out


# ---------------- 确定性大脑 ----------------

_call_ids = itertools.count(1)

FIB_CODE = (
    "a, b = 0, 1\n"
    "out = []\n"
    "for _ in range(10):\n"
    "    out.append(str(a))\n"
    "    a, b = b, a + b\n"
    "print(' '.join(out))\n"
)
FIB_SEQ = "0 1 1 2 3 5 8 13 21 34"


def _content(text):
    return {"content": text}


def _tool(name, args):
    return {"tool_calls": [{"name": name, "arguments": args}]}


def _last_user(messages):
    for m in reversed(messages):
        if m.get("role") == "user":
            return m.get("content") or ""
    return ""


def _after_last_user(messages):
    """最后一个 user 之后的消息（本轮的工具往返都在这里）。"""
    idx = max(i for i, m in enumerate(messages) if m.get("role") == "user")
    return messages[idx + 1:]


def _tool_results(messages):
    return [m.get("content") or "" for m in _after_last_user(messages)
            if m.get("role") == "tool"]


def _tool_calls(messages):
    calls = []
    for m in _after_last_user(messages):
        for tc in m.get("tool_calls") or []:
            fn = tc.get("function") or {}
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except ValueError:
                args = {}
            calls.append((fn.get("name", ""), args))
    return calls


def brain(messages):
    """看上下文决定下一步：返回 {"content": ...} 或 {"tool_calls": [...]}。

    规则按任务标记（评测 prompt 里的固定字样）分发；工具往返的
    阶段靠“最后一个 tool 结果长什么样”推进——和真模型看结果
    再决定下一步是同一个形态，只是这里完全确定性。
    """
    user = _last_user(messages)
    results = _tool_results(messages)
    last = results[-1] if results else ""

    # 被拒（高风险拒绝 / 审批卡拒绝 / 非流式需审批）→ 收尾，不重试。
    if any(k in last for k in ("被拒绝", "用户拒绝了", "需要人工确认")):
        return _content("好的，这次操作没有被批准，我不会执行。就按拒绝处理。")

    # 任务 9 第二步：问幸运数字——答案只能从 system 消息（长期记忆
    # 注入）里找；记忆没注入就老实说不知道。检查器就靠这个区分
    # “记忆真的进了上下文”与“蒙对了”。
    if "幸运数字" in user and "多少" in user:
        # 只许在 system 消息里找：那正是长期记忆的注入通道；把历史
        # user 消息也算上会把“用户刚说过”误判成“记忆生效”。
        haystack = "\n".join(m.get("content") or "" for m in messages
                             if m.get("role") == "system")
        m = re.search(r"幸运数字是\s*(\d+)", haystack)
        if m:
            return _content(f"你的幸运数字是 {m.group(1)}。")
        return _content("我还不知道你的幸运数字。")

    # 任务 9 第一步：把偏好写进长期记忆（中风险，流式下走审批卡）。
    if "长期记忆" in user or "MEMORY.md" in user:
        if not results:
            m = re.search(r"幸运数字是\s*(\d+)", user)
            num = m.group(1) if m else "7391"
            return _tool("write_file", {
                "path": "MEMORY.md",
                "content": f"# 长期记忆\n\n- 回复默认用中文，简洁直接。\n"
                           f"- 我的幸运数字是 {num}。\n",
            })
        return _content("已把这条偏好写进长期记忆，以后都会记得。")

    # 任务 8：填充轮只回“收到”；最后一轮复述本句标记（标记在本轮
    # user 里，永不被截断——检查器同时验 dropped_rounds 与标记）。
    m = re.search(r"(OMEGA-[A-Za-z0-9]+)", user)
    if m:
        return _content(f"本句中的标记是 {m.group(1)}。")
    m = re.search(r"(FILLER-\d+)", user)
    if "填充轮" in user and m:
        return _content(f"收到，{m.group(1)}。")

    # 任务 1：斐波那契——写脚本 → 运行 → 汇报。
    if "斐波那契" in user:
        if not results:
            return _tool("write_file", {"path": "eval_t1/fib.py", "content": FIB_CODE})
        if "已写入" in last:
            return _tool("exec_command", {"command": "python3 eval_t1/fib.py"})
        if "exit=0" in last:
            return _content(f"脚本已跑通：斐波那契数列前 10 项是 {FIB_SEQ}。")
        return _content("斐波那契任务处理中。")

    # 任务 2：修 bug——读文件 → 按报错把 a - b 改成 a + b → 运行验证。
    if "calc.py" in user:
        if not results:
            return _tool("read_file", {"path": "eval_t2/calc.py"})
        if "return a - b" in last:
            return _tool("write_file", {
                "path": "eval_t2/calc.py",
                "content": last.replace("return a - b", "return a + b"),
            })
        if "已写入" in last:
            return _tool("exec_command", {"command": "python3 eval_t2/calc.py"})
        if "exit=0" in last:
            return _content("已修复并验证：add(2, 3) 现在输出 5。")
        return _content("修 bug 处理中。")

    # 任务 3：批量改名——先看目录，再逐个 mv（exec 改名是中风险，
    # 流式下评测端会逐个点通过）。
    if "改名" in user or "old_1" in user:
        calls = _tool_calls(messages)
        mv_done = sum(1 for name, a in calls
                      if name == "exec_command" and "mv eval_t3" in a.get("command", ""))
        if not results:
            return _tool("list_files", {"path": "eval_t3"})
        if mv_done < 3 and ("exit=0" in last or ".txt" in last):
            i = mv_done + 1
            return _tool("exec_command", {
                "command": f"mv eval_t3/old_{i}.txt eval_t3/new_{i}.txt"})
        if mv_done >= 3:
            return _content("已把 old_1.txt、old_2.txt、old_3.txt 全部改名为 "
                            "new_1.txt、new_2.txt、new_3.txt，内容未动。")
        return _content("改名处理中。")

    # 任务 4：RAG 命中——检索后把结果带出处汇报。
    if "审批分几级" in user:
        if not results:
            return _tool("search_docs", {"query": "审批分几级"})
        if "未找到" in last:
            return _content("文档里没有找到审批分级的内容。")
        return _content("根据项目文档，审批分三级：只读自动执行、改动确认"
                        "一次、高危默认拒绝。检索到的原文如下：\n" + last[:800])

    # 任务 5：RAG 诚实未命中。
    if "红烧肉" in user:
        if not results:
            return _tool("search_docs", {"query": "红烧肉的做法"})
        if "未找到相关文档" in last:
            return _content("项目文档里未找到红烧肉的做法，文档中没有这部分内容。")
        return _content("检索结果如下：\n" + last[:500])

    # 任务 6：覆盖已有文件（中风险；评测端在审批卡上点拒绝）。
    if "eval_t6/target.txt" in user:
        if not results:
            return _tool("write_file", {"path": "eval_t6/target.txt",
                                        "content": "REPLACED\n"})
        return _content("覆盖操作未完成。")

    # 任务 7：删文件（高风险，应被默认拒绝）。
    if "keep.txt" in user:
        if not results:
            return _tool("exec_command", {"command": "rm eval_t7/keep.txt"})
        return _content("删除操作未完成。")

    # 任务 10：多轮中途改需求——按最后一句 user 里的目标词写脚本。
    if "greet.py" in user:
        word = "Bonjour" if "Bonjour" in user else "Hello"
        if not results:
            return _tool("write_file", {"path": "eval_t10/greet.py",
                                        "content": f'print("{word}")\n'})
        if "已写入" in last:
            return _tool("exec_command", {"command": "python3 eval_t10/greet.py"})
        if "exit=0" in last:
            return _content(f"已完成，脚本现在打印 {word}。")
        return _content("处理中。")

    return _content("好的。")


# ---------------- HTTP 服务 ----------------

def _assistant_message(result):
    msg = {"role": "assistant", "content": result.get("content")}
    if result.get("tool_calls"):
        msg["tool_calls"] = [
            {"id": f"call_{next(_call_ids)}", "type": "function",
             "function": {"name": tc["name"],
                          "arguments": json.dumps(tc["arguments"],
                                                  ensure_ascii=False)}}
            for tc in result["tool_calls"]
        ]
        msg["content"] = None
    return msg


def _sse_chunks(result):
    """把一次回复编码成 SSE delta 序列：正文分片发；工具调用的
    arguments 故意拆两片（考应用侧按 index 攒包，v0.2 的考点）。"""
    chunks = []
    if result.get("tool_calls"):
        tc = result["tool_calls"][0]
        args = json.dumps(tc["arguments"], ensure_ascii=False)
        half = max(1, len(args) // 2)
        call_id = f"call_{next(_call_ids)}"
        chunks.append({"tool_calls": [{"index": 0, "id": call_id, "type": "function",
                                       "function": {"name": tc["name"],
                                                    "arguments": args[:half]}}]})
        chunks.append({"tool_calls": [{"index": 0,
                                       "function": {"arguments": args[half:]}}]})
        finish = "tool_calls"
    else:
        text = result.get("content") or ""
        step = max(1, len(text) // 3)
        for i in range(0, len(text), step):
            chunks.append({"content": text[i:i + step]})
        finish = "stop"
    out = []
    for delta in chunks:
        out.append({"id": "chatcmpl-mock", "object": "chat.completion.chunk",
                    "choices": [{"index": 0, "delta": delta, "finish_reason": None}]})
    out.append({"id": "chatcmpl-mock", "object": "chat.completion.chunk",
                "choices": [{"index": 0, "delta": {}, "finish_reason": finish}]})
    return out


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # 静音访问日志
        pass

    def _send(self, body, content_type="application/json"):
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            body = {}
        if self.path.endswith("/embeddings"):
            texts = body.get("input") or []
            if isinstance(texts, str):
                texts = [texts]
            dims = int(body.get("dimensions") or 1024)
            vecs = fake_embed(texts, dims)
            payload = {
                "object": "list",
                "data": [{"object": "embedding", "index": i, "embedding": v}
                         for i, v in enumerate(vecs)],
                "model": body.get("model", "mock-embedding"),
                "usage": {"prompt_tokens": sum(len(t) for t in texts)},
            }
            return self._send(json.dumps(payload))
        if self.path.endswith("/chat/completions"):
            messages = body.get("messages") or []
            result = brain(messages)
            usage = {
                "prompt_tokens": sum(len(m.get("content") or "")
                                     for m in messages) // 2,
                "completion_tokens": 16,
            }
            usage["total_tokens"] = usage["prompt_tokens"] + 16
            if body.get("stream"):
                lines = "".join(
                    f"data: {json.dumps(c, ensure_ascii=False)}\n\n"
                    for c in _sse_chunks(result)
                ) + "data: [DONE]\n\n"
                return self._send(lines, "text/event-stream")
            payload = {
                "id": "chatcmpl-mock", "object": "chat.completion",
                "choices": [{"index": 0, "message": _assistant_message(result),
                             "finish_reason": "tool_calls" if result.get("tool_calls")
                             else "stop"}],
                "usage": usage,
            }
            return self._send(json.dumps(payload, ensure_ascii=False))
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()


class MockLLMServer:
    """在后台线程里跑的 mock LLM 服务（随机端口）。"""

    def __init__(self):
        self._srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.port = self._srv.server_address[1]
        self._thread = None

    @property
    def base_url(self):
        return f"http://127.0.0.1:{self.port}/v1"

    def start(self):
        import threading
        self._thread = threading.Thread(target=self._srv.serve_forever,
                                        daemon=True)
        self._thread.start()
        return self

    def stop(self):
        self._srv.shutdown()
        self._srv.server_close()
