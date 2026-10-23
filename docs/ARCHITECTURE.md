# 整体架构设计 —— Personal Agent v1

## 1. 目标与边界

- 用户通过网页（后续可 Tauri/Electron 套壳成桌面程序）和 AI 对话。
- AI 在用户自己的服务器上工作：读/写代码、执行命令、跑测试、做自动化。
- AI 本身不直接接触服务器：所有动作必须经工具层 + 权限层，留痕、可拦截。
- 模型可替换：只依赖 OpenAI-compatible 的 Chat Completions + Tool Calling 接口。

## 2. 分层架构

```
┌─────────────────────────────────────────────────────────┐
│  表现层 Presentation                                     │
│  static/index.html  单页聊天：消息流 + 工具调用日志展示    │
│  (后续: SSE 流式、审批弹窗、code-server 网页编辑器并排)    │
└──────────────────────┬──────────────────────────────────┘
                       │ HTTP  POST /api/chat   GET /health
┌──────────────────────▼──────────────────────────────────┐
│  编排层 Orchestration        backend/main.py (FastAPI)   │
│  - 拼上下文: System Prompt + 历史 + 工具 Schema          │
│  - Agent Loop: 调模型 → 有 tool_call 就执行 → 回喂结果    │
│    → 再调模型 … 直到无 tool_call 或达 MAX_TOOL_ROUNDS     │
│  - 鉴权: ACCESS_TOKEN (可选) / 超时 / 轮数上限            │
└───────┬──────────────────────────────┬────────────────┘
        │ tool_call (JSON)               │ HTTPS
┌───────▼──────────────────┐   ┌─────────▼────────────────┐
│  工具层 Tools             │   │  模型层 LLM (外部 API)    │
│  backend/tools.py        │   │  Muse / OpenAI / DeepSeek │
│  list_files / read_file  │   │  / Qwen / Moonshot …      │
│  write_file / exec_cmd   │   │  只做推理与决策，不执行    │
│  - 路径沙箱校验           │   └───────────────────────────┘
│  - 命令超时 60s / 输出截断 │
└───────┬──────────────────┘
        │ 仅允许访问
┌───────▼──────────────────┐
│  执行环境 Sandbox         │
│  agent_workspace/ (卷)   │
│  Docker 容器 / 低权用户   │
│  代码、数据、MEMORY.md    │
└──────────────────────────┘
```

## 3. 一次请求的时序

1. 浏览器 POST /api/chat {message, history}
2. 编排层组装 messages = [system, ...history, user]
3. 调用模型 API（带 tools schema）
4. 模型返回：
   - 纯文本 → 作为最终回复返回浏览器，结束
   - tool_calls → 编排层逐个调用工具层执行，把 {role: tool, content: 结果} 追加到 messages，回到第 3 步
5. 工具报错不中断：错误文本同样回喂，模型自行修正重试
6. 超过 MAX_TOOL_ROUNDS（默认 12）强制暂停，防止死循环烧 token

## 4. 数据与状态

| 数据 | 存放 | 说明 |
|---|---|---|
| 对话历史 | 前端内存（v1） | 刷新即丢；v2 落 SQLite |
| 代码/文件 | agent_workspace/ 卷 | Docker 重启不丢，AI 只能动这里 |
| 配置/密钥 | .env（不进 Git） | LLM_API_KEY、BASE_URL、MODEL、ACCESS_TOKEN |
| 长期记忆 | agent_workspace/MEMORY.md（预留） | v2 由编排层注入上下文 |

## 5. 安全设计

- **沙箱**：路径 resolve 后必须在 WORKSPACE 内，`../` 穿越直接拒绝（已实测）。
- **容器隔离**：生产部署用 docker-compose，后端跑在容器里，即使命令失控也出不了容器 + 卷的范围。
- **访问控制**：设置 ACCESS_TOKEN 后，/api/chat 必须带 `x-access-token` 请求头；公网部署再叠加反向代理 HTTPS。没有鉴权时只应绑定内网/VPN。
- **资源限制**：命令 60 秒超时、输出截断、工具轮数上限。
- **待补（v2）**：高风险操作人工审批（删除、对外网络请求）、工具调用审计日志落盘、命令白/黑名单。

## 6. 部署拓扑

```
用户浏览器 ──HTTPS/内网──► 服务器
                            ├─ docker-compose
                            │    ├─ agent (FastAPI, :8000) → LLM API (外网)
                            │    └─ code-server (可选 profile, :8080, 网页 VS Code)
                            └─ volume: agent_workspace/
```

本地开发不用 Docker 也行：`./start.sh` 直接 uvicorn 启动。

## 7. 扩展路线

- v1.1 流式输出 SSE + 工具日志实时推送
- v1.2 会话持久化 SQLite + MEMORY.md 长期记忆注入
- v1.3 审批卡：exec / 删除类工具执行前前端确认
- v2.0 工具层改 MCP Server，工具可被任意 MCP 客户端复用；增加 git、Playwright 浏览器、cron 定时工具
- v2.1 Tauri 打包桌面客户端（内嵌网页，连接远程后端）
