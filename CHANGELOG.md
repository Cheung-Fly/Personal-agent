# Changelog

## v0.2.0 — 2026-10-24

Streaming responses (SSE) with live tool logs.

- New `POST /api/chat/stream` endpoint: Server-Sent Events with three event
  types — `token` (incremental reply text), `tool` / `tool_result` (live tool
  execution log), and `done` (final reply). The original `/api/chat` is kept
  unchanged as a non-streaming fallback.
- Streaming tool-call accumulation: `tool_calls` arrive as fragmented deltas
  per index and are reassembled (id + name + arguments) before execution;
  results are fed back and the loop continues streaming. Verified against a
  mock streaming server (split deltas) and the real Zhipu GLM-4.6 API.
- Frontend consumes the stream via `ReadableStream`: tokens append to the
  reply bubble as they arrive, tool events insert log lines in order.

## v0.1.0 — 2026-10-23

First releasable version.

- Agent loop: FastAPI orchestrator calling an OpenAI-compatible LLM with tool calling
- Tools: `list_files`, `read_file`, `write_file`, `exec_command` in a sandboxed workspace
  (path-traversal blocking verified by tests)
- Web chat frontend with tool-call log display and ACCESS_TOKEN prompt
- `/health` endpoint; clear 502 error when the LLM API is misconfigured
- Docker + docker-compose deployment, optional code-server profile
- Architecture notes in `docs/ARCHITECTURE.md`
