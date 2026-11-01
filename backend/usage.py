"""成本统计：真实 usage 捕获、落盘与聚合（v0.10）。

设计口径（见 v0.10 设计草稿）：
- 每次 LLM 调用记一行 JSON Lines 到 data/usage.log，与 v0.5 审计日志
  同构（DATA_DIR 定位、不进 git、写失败不抛异常——记账不能拖垮主流程）。
- 每行分两种来源，靠 source 字段永远分开，不许混成一个“精确值”：
  - source="api"：数字来自 API 响应里的 usage（prompt/completion/
    total），真实消耗；
  - source="estimated"：API 没给 usage 时的回退，prompt_tokens 用
    context 的字符换算估算本次实际发送的 messages，completion /
    total 记 null（没测到的数不编）。
- 聚合 summarize_usage 给总计 + 按日（UTC）+ 按会话三维，形态同构，
  调用方各取所需。金额换算不做：单价随 provider 变，本版只到 token 层。

标准库，零新增依赖。
"""
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from .context import message_tokens


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
        "source": source,
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": total,
    }
    try:
        p = usage_log_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError as e:
        print(f"[usage] 写 usage 日志失败: {e}")
    return row


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
    """聚合 usage 流水：总计 + by_day（UTC 日期）+ by_session。

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
    for row in rows:
        _accumulate(summary, row)
        day = (row.get("ts") or "")[:10] or "unknown"
        _accumulate(summary["by_day"].setdefault(day, _blank()), row)
        sid = row.get("session_id") or "unknown"
        _accumulate(summary["by_session"].setdefault(sid, _blank()), row)
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
