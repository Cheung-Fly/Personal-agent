"""固定任务评测集（v0.8）测试：mock 模式链路验收 + 大脑规则单测。

口径先说清：这里断言的是“评测链路 10/10 跑通”——任务、检查器、
审批流、截断、记忆注入这些机制没断。mock 里的模型是确定性规则，
它的 10/10 不是 Agent 的真实成功率；真实成功率只看 live 基线
（eval/results/*-live.json）。两个数永远分开，不许混。
python -m pytest tests/ -q
"""
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "eval"))


def _load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "eval" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


run_eval = _load("run_eval")
mock_llm = _load("mock_llm")


def test_mock_eval_chain_all_pass(tmp_path):
    """全链路：mock LLM + 真实应用子进程，10 个任务逐个被检查器判通过。"""
    results = run_eval.run_mock(out_dir=tmp_path / "results",
                                sandbox_root=tmp_path / "sandbox")
    s = results["summary"]
    assert s["total"] == 10
    fails = [(t["id"], t["detail"]) for t in results["tasks"] if not t["passed"]]
    assert s["passed"] == 10, f"mock 链路未全通：{fails}"
    assert (tmp_path / "results").glob("*-mock.json")


def test_summarize_counts():
    rows = [
        {"passed": True, "prompt_tokens": 100, "seconds": 1.5},
        {"passed": False, "prompt_tokens": 250, "seconds": 2.5},
        {"passed": True, "prompt_tokens": 50, "seconds": 1.0},
    ]
    s = run_eval.summarize(rows)
    assert s == {"passed": 2, "total": 3, "success_rate": "2/3",
                 "total_prompt_tokens": 400, "total_seconds": 5.0}


# ---------- mock 大脑的确定性规则 ----------

def test_brain_fib_first_move_writes_script():
    out = mock_llm.brain([{"role": "user", "content": "请写斐波那契数列脚本"}])
    call = out["tool_calls"][0]
    assert call["name"] == "write_file"
    assert call["arguments"]["path"] == "eval_t1/fib.py"


def test_brain_memory_question_reads_only_system_messages():
    # 记忆生效：偏好在第二条 system 消息里 → 能答出数字
    msgs = [
        {"role": "system", "content": "你是一个个人 AI Agent。"},
        {"role": "system", "content": "长期记忆：\n- 我的幸运数字是 7391。"},
        {"role": "user", "content": "我的幸运数字是多少？"},
    ]
    assert "7391" in mock_llm.brain(msgs)["content"]
    # 记忆没注入（只有用户自己刚说过）→ 不许当成“记得”
    msgs2 = [
        {"role": "system", "content": "你是一个个人 AI Agent。"},
        {"role": "user", "content": "我的幸运数字是 7391，我的幸运数字是多少？"},
    ]
    assert "7391" not in mock_llm.brain(msgs2)["content"]


def test_brain_denial_ends_turn():
    msgs = [
        {"role": "user", "content": "把 eval_t6/target.txt 的内容改成 REPLACED"},
        {"role": "assistant", "content": None,
         "tool_calls": [{"id": "c1", "type": "function",
                         "function": {"name": "write_file", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c1",
         "content": "工具执行失败: 用户拒绝了本次 write_file 调用（覆盖已有文件）"},
    ]
    assert "不会执行" in mock_llm.brain(msgs)["content"]
