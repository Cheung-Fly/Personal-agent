# Changelog

## v0.3.0 — 2026-10-25

Persistent sessions (SQLite). Refresh the page or restart the server and
your history is still there.

- New `backend/storage.py` (stdlib `sqlite3`, no new dependencies): tables
  `sessions` (server-generated UUID, title from the first message) and
  `messages` (user/assistant rounds only — no internal tool messages, so
  there is no `tool_call_id` alignment problem on restore). Database file
  is `data/agent.db` (`DATA_DIR` env, `./data` by default); the
  docker-compose volume `./data:/app/data` keeps it across restarts, and
  `data/` is git-ignored.
- `ChatIn` gains optional `session_id`. Without one the server creates a
  new session and returns its id (it never pollutes an existing session);
  with one, the server's stored history takes priority over the
  frontend-sent history. New endpoints: `GET /api/sessions` (list) and
  `GET /api/sessions/{id}/messages` (restore). The stream emits a
  `session` event first and the `done` event carries `session_id`;
  `/api/chat` also persists and returns `session_id`.
- Frontend stores `session_id` in `localStorage` and restores the last
  rounds on load (unknown id → 404 → start a fresh session).
- History budget: only the most recent 20 rounds are loaded per request
  (`HISTORY_ROUNDS`, default 20); older rounds stay in SQLite, so
  changing N is a one-line change.
- Verified: pytest 7 passed (1 existing + 6 new storage tests); mock-LLM
  end-to-end checks passed (two streaming rounds, restore via GET,
  non-streaming without session id, unknown session id, 404 on unknown
  session, history still present after a uvicorn restart).

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
