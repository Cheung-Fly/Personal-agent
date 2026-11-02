"""成本统计：真实 usage 捕获、落盘与聚合（v0.10；v0.11 补 embedding）。

设计口径（见 v0.10 / v0.11 设计草稿）：
- 每次 LLM 调用记一行 JSON Lines 到 data/usage.log，与 v0.5 审计日志
  同构（DATA_DIR 定位、不进 git、写失败不抛异常——记账不能拖垮主流程）。
- 每行分两种来源，靠 source 字段永远分开，不许混成一个“精确值”：
  - source="api"：数字来自 API 响应里的 usage（prompt/completion/
    total），真实消耗；
  - source="estimated"：API 没给 usage 时的回退，prompt_tokens 用
    context 的字符换算估算本次实际发送的 messages，completion /
    total 记 null（没测到的数不编）。
- v0.11 起 embedding 调用也入账：行上加 kind 区分（"chat" / 
  "embedding"，老行没 kind 按 chat 读），embedding 行另带 purpose
  （"index" 索引构建 / "query" 查询向量化）。聚合 summarize_usage
  给总计 + 按日（UTC）+ 按会话 + 按 kind 分账，总计 = 聊天 +
  embedding 之和。金额换算不做：单价随 provider 变，只到 token 层。

标准库，零新增依赖。
"""
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from .context import estimate_tokens, message_tokens


def usage_log_path() -> Path:
    """usage 流水位置：data/usage.log（与 audit.log 同目录同风格）。"""
    return Path(os.environ.get("DATA_DIR", "./data")) / "usage.log"


def record_usage(*, session_id: str, model: str, stream: bool,
                 usage: dict | None, messages: list[dict] | None = None) -> dict:
    """记一次 LLM 调用的用量，返回落盘的那行（dict）。

    usage 是 API 响应里的 usage 字段（可为 None）；有 prompt_tokens
    就按真实值记 source="api"，否则按 messages 估算记
    source="estimated"——回退必须显式标注，是本版的诚实口径。
    """
    if usage and usage.get("prompt_tokens") is not None:
        prompt = int(usage["prompt_tokens"])
        completion = usage.get("completion_tokens")
        completion = int(completion) if completion is not None else None
        total = usage.get("total_tokens")
        total = int(total) if total is not None else (
            prompt + completion if completion is not None else None)
        source = "api"
    else:
        prompt = sum(message_tokens(m) for m in (messages or []))
        completion = None
        total = None
        source = "estimated"
    row = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "session_id": session_id,
        "model": model,
        "stream": bool(stream),
        "kind": "chat",
        "source": source,
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": total,
    }
    _append_row(row)
    return row


def _append_row(row: dict) -> None:
    """把一行流水追加到 usage.log；写失败只 print（记账不能拖垮主流程）。"""
    try:
        p = usage_log_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError as e:
        print(f"[usage] 写 usage 日志失败: {e}")


# v0.11：embedding 行的按 session 暂存——响应层（/api/chat 响应、流式
# done 事件）要回答“这个请求期间烧了多少 embedding”，落盘行是事后
# 查的流水，暂存是请求内的即时通道。record 时顺手塞暂存，
# take_embedding_usage 取走并清空。键是 session_id，单人单进程够用；
# 同 session 并发请求互串是已知边界（见 v0.11 设计草稿）。
_pending_embedding: dict[str, list[dict]] = {}


def record_embedding_usage(*, usage: dict | None, texts: list[str],
                           purpose: str, model: str,
                           session_id: str | None = None) -> dict:
    """记一次 embedding API 调用的用量，返回落盘的那行（dict）。

    与聊天记账同一本账（data/usage.log），靠 kind="embedding" 区分，
    purpose 记 "index"（索引构建）/ "query"（查询向量化）。
    embedding 接口的 usage 只有 prompt_tokens / total_tokens（不生成
    文本、没有 completion），所以 source="api" 时 completion_tokens
    记 0——API 的 total 本来就等于 prompt，这是测到的事实不是编数；
    没给 usage 时按 texts 字符换算估算并标 source="estimated"，
    completion / total 记 null，与聊天的回退口径一致。
    """
    if usage and usage.get("prompt_tokens") is not None:
        prompt = int(usage["prompt_tokens"])
        completion = 0
        total = usage.get("total_tokens")
        total = int(total) if total is not None else prompt
        source = "api"
    else:
        prompt = sum(estimate_tokens(t) for t in texts)
        completion = None
        total = None
        source = "estimated"
    row = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "session_id": session_id or "",
        "model": model,
        "stream": False,
        "kind": "embedding",
        "purpose": purpose,
        "source": source,
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": total,
    }
    _append_row(row)
    if session_id:
        _pending_embedding.setdefault(session_id, []).append(row)
    return row


def merge_embedding_totals(rows: list[dict]) -> dict:
    """把一个请求内多次 embedding 调用的行合成响应里的 embedding_usage
    字段：{calls, prompt_tokens, total_tokens, source}。

    source 口径同 merge_call_totals：全 api → "api"，全 estimated →
    "estimated"，混着 → "mixed"；total 只要有一行没测到就记 null。
    空列表（本请求没触发 embedding）返回全 0、source="api"——没消耗
    就没有估算成分，这样标不会把纯聊天请求误报成混合来源。
    """
    prompt = sum(r["prompt_tokens"] for r in rows)
    total = None
    if all(r.get("total_tokens") is not None for r in rows):
        total = sum(r["total_tokens"] for r in rows)
    sources = {r.get("source") for r in rows}
    if sources <= {"api"}:
        source = "api"
    elif sources == {"estimated"}:
        source = "estimated"
    else:
        source = "mixed"
    return {"calls": len(rows), "prompt_tokens": prompt,
            "total_tokens": total, "source": source}


def take_embedding_usage(session_id: str) -> dict:
    """取走某 session 暂存的 embedding 行并合并（见 _pending_embedding）。

    请求开始时先调一次丢弃残留（防上一个没正常收尾的请求串账），
    响应构造时再调一次拿本请求的账。没有暂存返回全 0 合计。
    """
    rows = _pending_embedding.pop(session_id, [])
    return merge_embedding_totals(rows)


def load_usage(path: Path | None = None) -> list[dict]:
    """读全部 usage 流水；文件不存在返回空列表，坏行跳过不报错。"""
    p = path or usage_log_path()
    if not p.exists():
        return []
    rows = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    return rows


def _blank() -> dict:
    return {"calls": 0, "api_calls": 0, "estimated_calls": 0,
            "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}


def _accumulate(bucket: dict, row: dict) -> None:
    bucket["calls"] += 1
    if row.get("source") == "api":
        bucket["api_calls"] += 1
    else:
        bucket["estimated_calls"] += 1
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        v = row.get(key)
        if v is not None:
            bucket[key] += int(v)


def summarize_usage(rows: list[dict] | None = None) -> dict:
    """聚合 usage 流水：总计 + by_day（UTC 日期）+ by_session + by_kind。

    每个 bucket 同构：{calls, api_calls, estimated_calls, prompt_tokens,
    completion_tokens, total_tokens}。token 合计只累加有值的行——
    estimated 行没有 completion/total，那部分就是没测到，不当 0 混进
    真实数里假装精确；api_calls 与 estimated_calls 的比例说明了
    合计里有多少是真的。
    """
    if rows is None:
        rows = load_usage()
    summary = _blank()
    summary["by_day"] = {}
    summary["by_session"] = {}
    # v0.11 分账：总计含聊天 + embedding 全部行；by_kind 两本子账，
    # 老行没有 kind 字段一律按 chat 读（v0.10 及更早的流水可读）。
    summary["by_kind"] = {"chat": _blank(), "embedding": _blank()}
    for row in rows:
        _accumulate(summary, row)
        day = (row.get("ts") or "")[:10] or "unknown"
        _accumulate(summary["by_day"].setdefault(day, _blank()), row)
        sid = row.get("session_id") or "unknown"
        _accumulate(summary["by_session"].setdefault(sid, _blank()), row)
        kind = row.get("kind") or "chat"
        _accumulate(summary["by_kind"].setdefault(kind, _blank()), row)
    return summary


def merge_call_totals(rows: list[dict]) -> dict:
    """把一个请求内多次调用的 record_usage 行合成响应里的 usage 字段：
    {prompt_tokens, completion_tokens, total_tokens, source}。

    source：全是 api → "api"；全是 estimated → "estimated"；
    混着 → "mixed"。completion/total 只要有一次调用没测到就记 null
    （部分和会低估，宁可明说没测全）。
    """
    prompt = sum(r["prompt_tokens"] for r in rows)
    completion = None
    total = None
    if rows and all(r.get("completion_tokens") is not None for r in rows):
        completion = sum(r["completion_tokens"] for r in rows)
    if rows and all(r.get("total_tokens") is not None for r in rows):
        total = sum(r["total_tokens"] for r in rows)
    sources = {r.get("source") for r in rows}
    if sources == {"api"}:
        source = "api"
    elif sources == {"estimated"}:
        source = "estimated"
    else:
        source = "mixed"
    return {"prompt_tokens": prompt, "completion_tokens": completion,
            "total_tokens": total, "source": source}
