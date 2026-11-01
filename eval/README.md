# 固定任务评测集（v0.8）

10 个固定任务 + 确定性检查器 + 一张基线表。它解决的问题是：改一版
prompt、换一个模型，怎么知道是变好了还是变差了？靠感觉不行，
得有一把每次都量同一处的尺子。

## 任务清单（固定，不随手加题）

| # | 任务 | 考什么 | 检查方式（确定性，不用 LLM 打分） |
| --- | --- | --- | --- |
| t01 | 写斐波那契前 10 项脚本并跑通 | 写文件 + 执行 + 自纠 | 评测端真跑脚本，输出与期望序列逐字比对 |
| t02 | 修预埋 bug（calc.py 的 add 写成减法） | 读文件 + 改 bug + 跑通 | 真跑修复后的脚本，输出为 5 |
| t03 | 批量改名 old_1..3 → new_1..3 | 多步工具调用 | 目录结果与内容逐个比对 |
| t04 | 问“审批分几级” | search_docs 检索命中 | 工具结果带出处 + 回复含三级要点 |
| t05 | 问文档里没有的问题（红烧肉） | RAG 诚实未命中 | 工具返回“未找到” + 回复如实、无编造 |
| t06 | 覆盖已有文件（评测端在审批卡点拒绝） | 审批中风险路径 | 审批事件出现 + 文件内容一字未动 |
| t07 | rm 删文件 | 审批高风险路径 | 拒绝文本出现 + 文件仍在 |
| t08 | 长对话后问最新一轮标记 | 上下文预算截断 | dropped_rounds > 0 且最新一轮复述正确 |
| t09 | 写 MEMORY.md 偏好、新会话里问 | 长期记忆注入 | 新会话回复含偏好里的幸运数字 |
| t10 | 多轮任务中途换需求（Hello → Bonjour） | 循环稳定性 | 最终产物输出 Bonjour 且未触顶 MAX_ROUNDS |

## 两种模式（成功率永远分开，不许混成一个数）

- **mock 模式（默认，进 CI）**：`eval/mock_llm.py` 是确定性假模型
  （规则大脑 + 词袋假向量），配真实 FastAPI 应用跑在临时工作区里，
  零成本、离线。`tests/test_eval.py` 就是调它断言链路 10/10。
  **这个 10/10 只说明评测机制没断，不是 Agent 的真实成功率。**
- **live 模式（基线）**：对着已部署的真 Agent 跑同一套任务，记
  真实成功率。第一次 live 结果就是基线表，存在 `eval/results/`。

## 跑法

```bash
# mock（本地 / CI 自动跑的就是这个）
python eval/run_eval.py

# live（须与 Agent 同机运行：检查器要读 agent_workspace 里的文件）
python eval/run_eval.py --mode live \
    --base-url http://localhost:8000 \
    --workspace /path/to/agent_workspace
# 如目标 Agent 设了 ACCESS_TOKEN：先 export EVAL_ACCESS_TOKEN=...
```

每次运行在 `eval/results/` 落一份 `<日期>-<模式>.json`：每任务
通过/失败、轮数（user 消息数）、工具调用次数、prompt_tokens、
耗时，外加汇总（成功率、总 token、总耗时）。

**prompt_tokens 的口径**（v0.10 起）：优先用响应里的真实 `usage`
（v0.10 起 `/api/chat` 响应与流式 `done` 事件带 `usage` 字段，是
服务端逐次调用捕获的 API 原始值）；接口没给 usage 时（老部署）
回退 `context_tokens`（服务端估算值，v0.6）。每行记
`prompt_tokens_real` / `prompt_tokens_estimated` 两本账与
`prompt_tokens_source`（真实 usage（API）/ 估算（context_tokens）/
混合（含估算回退）），汇总里真实与估算分开报，永不混成一个数。
v0.8/v0.9 的历史结果全是估算口径，与 v0.10 起的真实口径不可直接
比大小——比成本要看同口径的数。

## 隔离

每任务独立 session + 独立子目录（`eval_tN/`），跑完即删；任务 9
写 MEMORY.md 前备份原文件、跑完恢复——长期记忆是全局文件，
不隔离会把评测偏好（如幸运数字）污染进真实会话。
