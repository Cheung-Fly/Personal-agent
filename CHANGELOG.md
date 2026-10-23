# Changelog

## v0.1.0 — 2026-10-23

First releasable version.

- Agent loop: FastAPI orchestrator calling an OpenAI-compatible LLM with tool calling
- Tools: `list_files`, `read_file`, `write_file`, `exec_command` in a sandboxed workspace
  (path-traversal blocking verified by tests)
- Web chat frontend with tool-call log display and ACCESS_TOKEN prompt
- `/health` endpoint; clear 502 error when the LLM API is misconfigured
- Docker + docker-compose deployment, optional code-server profile
- Architecture notes in `docs/ARCHITECTURE.md`
