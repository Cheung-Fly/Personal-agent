"""成本统计（v0.10）单测：真实 usage 记录、估算回退标注、聚合分账。

口径：API 给了 usage 就记真实值（source="api"），没给就用字符换算
估算并明确标 source="estimated"，completion/total 记 null 不编数；
聚合时真实与估算靠 api_calls / estimated_calls 分开，token 合计只
累加有值的行。函数内导入 backend.* 的原因见 test_prompt.py 注释
（导入时机与 monkeypatch 的窗口问题）。
"""
import json


def _rows(tmp_path):
    return [json.loads(line) for line in
            (tmp_path / "usage.log").read_text(encoding="utf-8").splitlines()
            if line.strip()]


def test_record_api_usage_writes_real_numbers(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    from backend.usage import record_usage

    row = record_usage(session_id="s1", model="glm-4.6", stream=False,
                       usage={"prompt_tokens": 120, "completion_tokens": 30,
                              "total_tokens": 150},
                       messages=[{"role": "user", "content": "hi"}])
    assert row["source"] == "api"
    assert row["prompt_tokens"] == 120
    assert row["completion_tokens"] == 30
    assert row["total_tokens"] == 150
    assert row["stream"] is False
    # 落盘的与返回的是同一行
    assert _rows(tmp_path) == [row]


def test_record_api_usage_total_fallback_is_sum(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    from backend.usage import record_usage

    row = record_usage(session_id="s1", model="m", stream=True,
                       usage={"prompt_tokens": 10, "completion_tokens": 4},
                       messages=[])
    assert row["total_tokens"] == 14  # total 缺失时用 prompt+completion 补


def test_record_without_usage_falls_back_to_estimate(tmp_path, monkeypatch):
    """流式没等到 usage 的情形：回退估算且必须标 estimated，
    completion/total 为 null——没测到的数不许编。"""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    from backend.usage import record_usage

    row = record_usage(session_id="s2", model="m", stream=True, usage=None,
                       messages=[{"role": "user", "content": "你好世界"},
                                 {"role": "assistant", "content": "hello world"}])
    assert row["source"] == "estimated"
    assert row["prompt_tokens"] > 0  # 字符换算估算，非 0 非真实值冒充
    assert row["completion_tokens"] is None
    assert row["total_tokens"] is None


def test_record_empty_usage_dict_also_falls_back(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    from backend.usage import record_usage

    row = record_usage(session_id="s2", model="m", stream=False, usage={},
                       messages=[{"role": "user", "content": "abcd"}])
    assert row["source"] == "estimated"
    assert row["prompt_tokens"] == 1  # 4 英文字符 ≈ 1 token


def test_summarize_aggregates_total_by_day_by_session(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    from backend.usage import record_usage, summarize_usage

    record_usage(session_id="s1", model="m", stream=False,
                 usage={"prompt_tokens": 100, "completion_tokens": 20,
                        "total_tokens": 120}, messages=[])
    record_usage(session_id="s1", model="m", stream=True,
                 usage={"prompt_tokens": 50, "completion_tokens": 10,
                        "total_tokens": 60}, messages=[])
    record_usage(session_id="s2", model="m", stream=True, usage=None,
                 messages=[{"role": "user", "content": "你好世界"}])

    s = summarize_usage()
    assert s["calls"] == 3
    assert s["api_calls"] == 2 and s["estimated_calls"] == 1
    assert s["prompt_tokens"] == 150 + 3  # 估算行“你好世界”≈3 一并计入 prompt
    assert s["completion_tokens"] == 30  # 只累加真实测到的
    assert s["total_tokens"] == 180
    assert s["by_session"]["s1"]["calls"] == 2
    assert s["by_session"]["s1"]["total_tokens"] == 180
    assert s["by_session"]["s2"]["estimated_calls"] == 1
    days = list(s["by_day"].values())
    assert len(days) == 1 and days[0]["calls"] == 3  # 同一天（UTC）


def test_summarize_empty_log(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    from backend.usage import summarize_usage

    s = summarize_usage()
    assert s["calls"] == 0 and s["prompt_tokens"] == 0
    assert s["by_day"] == {} and s["by_session"] == {}


def test_merge_call_totals_source_labels():
    from backend.usage import merge_call_totals

    api = {"source": "api", "prompt_tokens": 10, "completion_tokens": 5,
           "total_tokens": 15}
    est = {"source": "estimated", "prompt_tokens": 7,
           "completion_tokens": None, "total_tokens": None}
    assert merge_call_totals([api, api]) == {
        "prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30,
        "source": "api"}
    mixed = merge_call_totals([api, est])
    assert mixed["source"] == "mixed"
    assert mixed["prompt_tokens"] == 17
    assert mixed["completion_tokens"] is None  # 有一次没测到就不给部分和
    assert merge_call_totals([est])["source"] == "estimated"


# ---------- v0.11：embedding 入账、分账与 per-request 暂存 ----------

def test_record_embedding_api_usage_writes_row(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    from backend.usage import record_embedding_usage

    row = record_embedding_usage(
        usage={"prompt_tokens": 2318, "total_tokens": 2318},
        texts=["块一", "块二"], purpose="index", model="embedding-3",
        session_id="s1")
    assert row["kind"] == "embedding" and row["purpose"] == "index"
    assert row["source"] == "api"
    assert row["prompt_tokens"] == 2318
    # embedding 不生成文本：API 的 total 就等于 prompt，completion 记 0
    # 是测到的事实，不是编数（与没测到时记 null 区分）。
    assert row["completion_tokens"] == 0
    assert row["total_tokens"] == 2318
    assert _rows(tmp_path) == [row]


def test_record_embedding_total_fallback_and_estimated(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    from backend.usage import record_embedding_usage

    row = record_embedding_usage(usage={"prompt_tokens": 40}, texts=["x"],
                                 purpose="query", model="m")
    assert row["source"] == "api" and row["total_tokens"] == 40  # total 缺失用 prompt 补

    est = record_embedding_usage(usage=None, texts=["你好世界"],
                                 purpose="query", model="m")
    assert est["source"] == "estimated"
    assert est["prompt_tokens"] == 3  # 与聊天估算同一口径（4 汉字 ≈ 3）
    assert est["completion_tokens"] is None and est["total_tokens"] is None


def test_summarize_by_kind_and_legacy_rows():
    """by_kind 分账：总计 = 聊天 + embedding；v0.10 前的老行没有
    kind 字段，必须按 chat 读（老流水可读是硬约束）。"""
    from backend.usage import summarize_usage

    legacy_chat = {"ts": "2026-11-02T00:00:00+00:00", "session_id": "s0",
                   "model": "glm-4.6", "stream": False, "source": "api",
                   "prompt_tokens": 10, "completion_tokens": 2,
                   "total_tokens": 12}
    emb = {"ts": "2026-11-02T00:01:00+00:00", "session_id": "s0",
           "model": "embedding-3", "stream": False, "kind": "embedding",
           "purpose": "query", "source": "api", "prompt_tokens": 5,
           "completion_tokens": 0, "total_tokens": 5}
    s = summarize_usage(rows=[legacy_chat, emb])
    assert s["calls"] == 2
    assert s["prompt_tokens"] == 15 and s["total_tokens"] == 17
    assert s["by_kind"]["chat"]["calls"] == 1
    assert s["by_kind"]["chat"]["total_tokens"] == 12
    assert s["by_kind"]["embedding"]["calls"] == 1
    assert s["by_kind"]["embedding"]["prompt_tokens"] == 5


def test_take_embedding_usage_merges_and_drains(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    from backend.usage import record_embedding_usage, take_embedding_usage

    record_embedding_usage(usage={"prompt_tokens": 10, "total_tokens": 10},
                           texts=["a"], purpose="index", model="m",
                           session_id="sx")
    record_embedding_usage(usage=None, texts=["你好世界"], purpose="query",
                           model="m", session_id="sx")
    merged = take_embedding_usage("sx")
    assert merged["calls"] == 2 and merged["prompt_tokens"] == 13
    assert merged["source"] == "mixed"  # 一真一估算，混着就标 mixed
    assert merged["total_tokens"] is None  # 估算行没测到 total，不给部分和
    # 取走即清空：第二次取是全 0（没消耗就没有估算成分，标 api 无害）
    assert take_embedding_usage("sx") == {
        "calls": 0, "prompt_tokens": 0, "total_tokens": 0, "source": "api"}
    # 没带 session 的行不进暂存（无从归属到请求）
    record_embedding_usage(usage={"prompt_tokens": 1, "total_tokens": 1},
                           texts=["a"], purpose="query", model="m")
    assert take_embedding_usage("sx")["calls"] == 0
