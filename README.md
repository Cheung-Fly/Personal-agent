# Personal Agent

> A personal AI agent that lives on your own server — chat with it from the browser, and it writes code, runs commands, and automates multi-step tasks for you in a sandboxed workspace.
>
> 一个住在你自己服务器上的个人 AI Agent：在浏览器里和它对话，它就能在沙箱工作区里写代码、跑命令、完成多步自动化任务。

[![CI](https://github.com/Cheung-Fly/Personal-agent/actions/workflows/ci.yml/badge.svg)](https://github.com/Cheung-Fly/Personal-agent/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/python-3.12-blue)
![License: MIT](https://img.shields.io/badge/License-MIT-green)

## 演示 / Demo

![Demo: the agent writes hello.py, hits an error, fixes it itself, and runs it successfully](docs/demo.gif)

> 真实运行记录：Agent 写下 `hello.py`，第一次运行失败后自己换 `python3` 重试成功。
> Recorded from a real run: the agent writes `hello.py`, fails once, corrects itself, and succeeds.

## Why / 为什么做这个

Chatbots only talk. This project explores the agent pattern behind products like Muse:
an LLM that **decides**, a runtime that **orchestrates**, and tools that **act** — with sandboxing and human control in between.

普通聊天机器人只会说。这个项目复刻并拆解了 Muse 这类产品的 Agent 模式：大模型负责决策、编排层负责循环、工具层负责真正动手，三者之间有沙箱和权限兜底。

## Features / 功能

- **Web chat UI** — no build step, plain HTML/JS served by the backend / 零构建的网页聊天界面
- **Agent loop** — model → tool calls → results fed back → repeat, with a max-round guard / 模型与工具的多轮循环，带轮数上限防死循环
- **Server-side tools** — `list_files` / `read_file` / `write_file` / `exec_command` / `search_docs` / 在服务器上读写文件、执行命令、检索文档
- **Sandboxed workspace** — path-traversal protection, command timeout, output truncation / 路径沙箱校验、命令超时、输出截断
- **Model-agnostic** — any OpenAI-compatible API with tool calling (Muse / OpenAI / DeepSeek / Qwen…) / 任意支持 tool calling 的兼容接口，改配置即换模型
- **Persistent sessions (SQLite)** — refresh or restart and history is still there; server-generated session id, last 20 rounds loaded per request / 会话持久化：刷新、重启都不丢历史
- **Long-term memory (MEMORY.md)** — hand-written preferences injected as a second system message on every request; edit the file and the next message follows it / 长期记忆：手动写偏好，每次对话自动注入，改完下一句就生效
- **Approval tiers (v0.5)** — read-only tools auto-run, changes ask once on an approval card, dangerous calls are refused by default; every tool call is written to an audit log (`data/audit.log`) / 审批分级：只读自动、改动弹卡确认、高危默认拒绝，每次调用记审计日志
- **Doc search / small RAG (v0.7)** — a `search_docs` tool retrieves the most relevant passages from the project docs (`agent_workspace/docs/*.md`) by embedding similarity (Zhipu `embedding-3`, 1024-dim, vectors cached on disk and re-embedded per changed chunk), returns at most 3 sourced passages (~2000 chars), and honestly reports "not found" below a similarity threshold instead of guessing / 小 RAG：按语义检索项目文档，只喂最相关的几段，未命中老实说找不到
- **Context budget (v0.6)** — token estimation plus a budget (`CONTEXT_TOKEN_BUDGET`, default 8000): oldest history rounds are dropped when the context would overflow, system prompt / memory / latest round never dropped; `done` reports `context_tokens` and `dropped_rounds`, real `usage.prompt_tokens` is logged / 上下文预算：字符换算估算 token，超预算从最旧轮次整轮截断，`done` 事件带估算 token 数与丢弃轮数
- **Fixed-task eval set (v0.8)** — 10 fixed tasks (scripting, bug fix, batch rename, doc-search hit / honest miss, approval deny paths, context truncation, long-term memory, mid-task requirement change) graded by deterministic checkers, never by an LLM judge; mock mode runs in CI to verify the harness, live mode against the deployed agent produces the baseline table in `eval/results/` / 固定任务评测集：确定性检查判定、不用 LLM 当裁判，mock 验链路进 CI，live 对真机出基线表
- **Access token auth** + Docker deployment, optional web VS Code (code-server) side-by-side / 口令鉴权、Docker 一键部署、可选网页版 VS Code

## Architecture / 架构

```
Browser (static/index.html)
    │  POST /api/chat
    ▼
Orchestrator (FastAPI, backend/main.py)  ──►  LLM API (external, decides only)
    │  agent loop: tool_call → execute → feed result back
    ▼
Tools (backend/tools.py)  ──►  Sandboxed workspace (agent_workspace/)
```

Full design notes: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)（中文）

## Quick Start / 快速开始

**Docker (recommended / 推荐):**

```bash
git clone https://github.com/Cheung-Fly/Personal-agent.git
cd Personal-agent
cp .env.example .env   # fill in LLM_API_KEY / LLM_BASE_URL / LLM_MODEL / ACCESS_TOKEN
docker compose up -d --build
# open http://localhost:8000
```

**Local / 本地运行:**

```bash
cp .env.example .env
pip install -r requirements.txt
./start.sh             # http://localhost:8000
```

Check it's alive: `curl http://localhost:8000/health`

## Configuration / 配置 (.env)

| Key | Description |
|---|---|
| `LLM_API_KEY` | API key for your model provider / 模型 API 密钥（只放服务器，不要提交 Git） |
| `LLM_BASE_URL` | OpenAI-compatible base URL, e.g. `https://api.openai.com/v1` |
| `LLM_MODEL` | Model name that supports tool calling |
| `ACCESS_TOKEN` | Password for `/api/chat` — **set this before exposing the server** / 暴露到公网前必须设置 |
| `WORKSPACE_DIR` | Where the agent may work (default `./agent_workspace`) |
| `DATA_DIR` | Where the SQLite session DB lives (default `./data`, file `agent.db`) |
| `HISTORY_ROUNDS` | Most recent rounds loaded per request (default `20`) |
| `MEMORY_MAX_CHARS` | Long-term memory characters injected per request (default `2000`) |
| `APPROVAL_TIMEOUT_SECONDS` | How long an approval card waits before counting as denied (default `300`) |
| `CONTEXT_TOKEN_BUDGET` | Context budget in estimated tokens (default `8000`, history + memory only; oldest rounds are dropped when over budget) / 上下文预算，超了从最旧轮次截断 |
| `EMBEDDING_MODEL` / `EMBEDDING_DIMENSIONS` | Embedding model for doc search (default `embedding-3` / `1024`; shares `LLM_API_KEY` and `LLM_BASE_URL`) |
| `RAG_DOCS_DIR` | Doc corpus for `search_docs` (default `WORKSPACE_DIR/docs`) |
| `RAG_TOP_K` / `RAG_MIN_SCORE` | Passages returned per search (default `3`) / minimum cosine similarity (default `0.3`; below it the tool honestly returns "not found") |
| `MAX_TOOL_ROUNDS` | Agent loop limit (default `12`) |

## Security / 安全

- The agent can run shell commands **inside its workspace/container only**. Never expose it to the public internet without `ACCESS_TOKEN` + HTTPS (or keep it on a private network / VPN).
- API keys live in `.env`, which is git-ignored and docker-ignored.
- Roadmap: human approval for destructive actions, audit logging, command allow-list — **done in v0.5** (approval tiers + `data/audit.log`).

## Roadmap / 路线图

- [x] v0.1 Agent loop + sandboxed file/shell tools + web chat
- [x] v0.2 Streaming responses (SSE) with live tool logs
- [x] v0.3 Persistent sessions (SQLite)
- [x] v0.4 Long-term memory file (MEMORY.md injected as second system message)
- [x] v0.5 Approval tiers for tool calls (read-only auto, changes on a card, dangerous refused) + audit log
- [x] v0.6 Context budget (token estimation + oldest-round truncation, `CONTEXT_TOKEN_BUDGET` default 8000) + real `usage` logging
- [x] v0.7 Small RAG: `search_docs` tool with embedding-based retrieval over workspace docs (cached vectors, honest not-found)
- [x] v0.8 Fixed-task eval set: 10 tasks with deterministic checkers, mock mode in CI + first live baseline; `RAG_MIN_SCORE` default recalibrated to 0.4
- [ ] MCP server for the tool layer; git & browser (Playwright) tools
- [ ] Desktop shell (Tauri)

## Project structure / 目录

```
personal-agent/
├─ backend/          # orchestrator (main.py) + tools (tools.py) + sessions (storage.py) + long-term memory (memory.py) + approval tiers (approval.py) + context budget (context.py) + doc search (rag.py)
├─ eval/             # fixed-task eval set (v0.8): task definitions + deterministic checkers + mock/live runners + baseline results
├─ static/           # web chat frontend
├─ agent_workspace/  # the only place the agent can touch (persisted volume)
├─ tests/            # tool-layer tests, no LLM required
├─ docs/             # architecture notes
├─ Dockerfile / docker-compose.yml
└─ .env.example
```

## Tests / 测试

```bash
pip install pytest
python -m pytest tests/ -q   # covers write/read/exec + sandbox path-traversal blocking
python eval/run_eval.py      # v0.8 eval set, mock mode (deterministic fake model, zero cost)
```

## License

MIT — see [LICENSE](LICENSE).
