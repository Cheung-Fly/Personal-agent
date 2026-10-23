# Personal Agent

> A personal AI agent that lives on your own server — chat with it from the browser, and it writes code, runs commands, and automates multi-step tasks for you in a sandboxed workspace.
>
> 一个住在你自己服务器上的个人 AI Agent：在浏览器里和它对话，它就能在沙箱工作区里写代码、跑命令、完成多步自动化任务。

[![CI](https://github.com/Cheung-Fly/Personal-agent/actions/workflows/ci.yml/badge.svg)](https://github.com/Cheung-Fly/Personal-agent/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/python-3.12-blue)
![License: MIT](https://img.shields.io/badge/License-MIT-green)

<!-- TODO: 发布后把这里换成演示 GIF：录一段「在网页里让 Agent 写一个脚本并跑出结果」的 30 秒操作 -->
<!-- Demo GIF goes here -->

## Why / 为什么做这个

Chatbots only talk. This project explores the agent pattern behind products like Muse:
an LLM that **decides**, a runtime that **orchestrates**, and tools that **act** — with sandboxing and human control in between.

普通聊天机器人只会说。这个项目复刻并拆解了 Muse 这类产品的 Agent 模式：大模型负责决策、编排层负责循环、工具层负责真正动手，三者之间有沙箱和权限兜底。

## Features / 功能

- **Web chat UI** — no build step, plain HTML/JS served by the backend / 零构建的网页聊天界面
- **Agent loop** — model → tool calls → results fed back → repeat, with a max-round guard / 模型与工具的多轮循环，带轮数上限防死循环
- **Server-side tools** — `list_files` / `read_file` / `write_file` / `exec_command` / 在服务器上读写文件、执行命令
- **Sandboxed workspace** — path-traversal protection, command timeout, output truncation / 路径沙箱校验、命令超时、输出截断
- **Model-agnostic** — any OpenAI-compatible API with tool calling (Muse / OpenAI / DeepSeek / Qwen…) / 任意支持 tool calling 的兼容接口，改配置即换模型
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
| `MAX_TOOL_ROUNDS` | Agent loop limit (default `12`) |

## Security / 安全

- The agent can run shell commands **inside its workspace/container only**. Never expose it to the public internet without `ACCESS_TOKEN` + HTTPS (or keep it on a private network / VPN).
- API keys live in `.env`, which is git-ignored and docker-ignored.
- Roadmap: human approval for destructive actions, audit logging, command allow-list.

## Roadmap / 路线图

- [x] v0.1 Agent loop + sandboxed file/shell tools + web chat
- [ ] Streaming responses (SSE) with live tool logs
- [ ] Persistent sessions (SQLite) + long-term memory file
- [ ] Approval cards for high-risk tools
- [ ] MCP server for the tool layer; git & browser (Playwright) tools
- [ ] Desktop shell (Tauri)

## Project structure / 目录

```
personal-agent/
├─ backend/          # orchestrator (main.py) + tools (tools.py)
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
```

## License

MIT — see [LICENSE](LICENSE).
