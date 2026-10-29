# Changelog

## v0.7.0 — 2026-10-29

Small RAG: the agent can now search the project documentation by
meaning and answer from retrieved passages instead of guessing from
the model's memory or reading whole files into the context.

- New `backend/rag.py` + a `search_docs` tool (stdlib + the existing
  httpx, no new dependencies). Corpus: `.md` files under
  `agent_workspace/docs/` (`RAG_DOCS_DIR` to change), read-only;
  `MEMORY.md` is excluded (v0.4 already injects it every turn, so
  indexing it would feed the same text twice). Chunking: split on
  Markdown headings first, then split over-long sections at 800
  characters with 100 characters of overlap so a sentence cut in half
  still matches from either side. The repo's own
  `docs/ARCHITECTURE.md` is copied into `agent_workspace/docs/` as
  the demo corpus (with a `.gitignore` exception, same pattern as
  MEMORY.md in v0.4).
- Retrieval uses the Zhipu embedding API — a deliberate change from
  the design draft, which recommended local TF-IDF; the user chose
  semantic search (decided 2026-10-29). `POST {LLM_BASE_URL}/embeddings`
  with `EMBEDDING_MODEL` (default `embedding-3`) and
  `EMBEDDING_DIMENSIONS` (default 1024), sharing the chat API's base
  URL and key, batched at most 64 inputs per call. Ranking is pure
  Python cosine similarity; `search_docs` is classified low-risk
  (auto-execute, same tier as `list_files` / `read_file`), returns
  at most `RAG_TOP_K` (default 3) passages with their source file
  and heading, capped at about 2000 characters total so a search
  cannot blow the v0.6 context budget.
- Honest miss: if the best score is below `RAG_MIN_SCORE` (default
  cosine 0.3), the tool returns an explicit "no relevant document
  found" text (with the best score) instead of padding the context
  with irrelevant passages — half of hallucination prevention lives
  on the retrieval side.
- Index cache: vectors are stored in `data/rag_index.json` keyed by
  content hash, with each chunk's source file, mtime and the model /
  dimensions recorded. Editing a document re-embeds only the chunks
  that changed; changing the embedding model or dimensions rebuilds
  the whole index (vectors from different models don't live in the
  same space). A response `usage` is logged when present.
- Failure behaviour: no API key, an embeddings endpoint error, or an
  empty corpus all return a clear failure text to the model (the
  v0.1 feed-the-error-back pattern) — the tool never raises into the
  agent loop, and the reply suggests falling back to `read_file` or
  answering honestly.
- Honest limits, documented: every query costs one embeddings API
  call (plus re-embedding changed chunks), so search has latency and
  a small per-call cost that keyword search would not; the local
  verification used a mock embeddings service (the vault key cannot
  be exported), so real-API embedding quality is verified after
  deployment, not locally.
- Verified: pytest 59 passed (45 existing + 14 new RAG tests:
  heading chunking, long-section split with overlap, MEMORY.md /
  non-Markdown exclusion, cache reuse and per-chunk invalidation,
  full rebuild on model change, hit with source attribution, honest
  miss, empty/missing corpus, result cap, failure texts instead of
  exceptions, cosine edge cases, low-risk classification);
  mock end-to-end 9/9 passed (mock LLM issuing a `search_docs` call
  with streamed, split arguments + mock embeddings endpoint against
  the real FastAPI app: no approval card for the low-risk tool,
  retrieved passage with source fed back to the model, final reply
  contains the documented content, audit log records low/auto,
  index file persisted, and an unrelated question returns the
  honest not-found text). An earlier e2e run also exercised the
  failure path for real: a mock returning the wrong vector
  dimension was rejected with a clear failure text and the loop
  completed normally.

## v0.6.0 — 2026-10-28

Context management: token estimation and a budget that truncates the
oldest history rounds before they reach the model.

- New `backend/context.py` (stdlib only, no new dependencies):
  `estimate_tokens` converts characters to tokens with a mixed
  heuristic — about 4 characters per token for English, about
  1.5–2 per token for Chinese (1.75 in the middle) — explicitly an
  estimate, not exact tokenization (there is no stdlib tokenizer).
  `truncate_history` drops the oldest complete rounds (user +
  assistant together) until history + long-term memory fits the
  budget; the system prompt, the memory message and the latest round
  are never dropped. `apply_budget` is the single integration point
  over the assembled `[system, memory?, *history, user]` list.
- Budget: `CONTEXT_TOKEN_BUDGET` (default 8000 tokens) counts history
  + memory only — the system prompt and the current user message are
  always in and never counted. At 20 rounds ≈ 6k tokens a normal
  conversation never triggers it; it only bites when long rounds or
  big tool outputs inflate the context. Storage is untouched
  (`storage.py` not modified): all 20 rounds stay in SQLite, only
  the assembled context is trimmed.
- Visibility: the streaming `done` event now carries `context_tokens`
  (estimated, budget scope) and `dropped_rounds` (how many old rounds
  were dropped this turn); `/api/chat` returns the same two fields.
  A truncation also logs one line (`[context] 超预算，丢弃 N 轮旧历史`).
  The frontend needs no change — it ignores the extra fields.
- Real usage is now recorded: `call_llm` (via `call_llm_with_usage`)
  keeps the response `usage.prompt_tokens` and logs one line per turn
  (`[context] 真实 prompt_tokens=...`); if the provider returns no
  `usage`, the estimated `context_tokens` is used as the fallback.
  Verified against the real Zhipu GLM-4.6 API, which returns `usage`
  with `prompt_tokens`. The numbers are logged only — cost statistics
  stay in the roadmap's third segment, where these logs get used.
- Honest limits, documented: the token count is a character-based
  heuristic, so the budget is approximate, not exact; truncation only
  happens at context-assembly time, so tool results produced inside
  the current turn are not trimmed mid-loop; streaming usage is not
  captured yet (that needs `stream_options: include_usage`, unverified
  with GLM).
- Verified: pytest 45 passed (29 existing + 16 new context tests:
  estimation in English/Chinese/mixed, truncation order, latest-round
  protection, memory counted but never dropped, shared
  `_build_messages` path); mock-LLM end-to-end 13 checks passed
  (25 rounds with a 200-token budget: `dropped_rounds` and
  `context_tokens` match the value computed from the real history,
  latest round kept, memory message present, storage still holds the
  last 20 rounds, real `usage` logged; default budget drops nothing).

## v0.5.0 — 2026-10-27

Approval tiers: tool calls are classified before they run — read-only
auto-executes, changes ask once on an approval card, dangerous calls
are refused by default.

- New `backend/approval.py` (stdlib only, no new dependencies): three
  levels — `low` auto-executes (`list_files`, `read_file`, read-only
  commands like `ls` / `cat` / `git status` / `git diff`, and `pytest`,
  which runs tests automatically by the user's choice), `medium` needs
  one confirmation (overwriting an existing file, running code like
  `python xxx.py`, `git commit`, writing `MEMORY.md`), `high` is
  refused with no card (`rm`, `sudo`, `chmod 777`, downloading and
  piping straight into a shell, sensitive paths like `.env` / `.pem`).
  Classification looks only at the first token plus a dangerous-flag
  list — no full shell parsing; unparseable commands and chained
  commands (`&&` / `;` / `|`) are escalated one level, so a suspicious
  command gets a card (or a refusal) rather than a free pass.
- Approval flow (streaming only): a medium-risk call pauses the loop and
  emits an SSE `approval_required` event (`approval_id`, tool, args,
  level, reason); the frontend shows a card with Approve / Deny. Approve
  runs that one call only — no "always allow". Deny feeds a failure text
  back to the model, which retries with another plan (reusing the
  v0.1 failure-text self-correction; no exception, no 500, the loop
  keeps going). Pending cards live in an in-memory dict; 5 minutes with
  no decision counts as a denial (`APPROVAL_TIMEOUT_SECONDS`, default
  300), and a restart drops unconfirmed cards — the safe default is
  always deny. The non-streaming `/api/chat` cannot wait for a human,
  so medium/high-risk calls there are denied in the reply text with a
  note that streaming can approve them.
- `MEMORY.md` auto-write opens halfway (v0.4 left this door shut): the
  agent may propose a memory write, but it always goes through a medium
  approval card and only lands if the user approves; otherwise the
  manual-edit entry stays as it was. The tool layer still refuses
  unapproved `MEMORY.md` writes even on a direct call.
- Audit log: every tool call appends one JSON Lines row to
  `data/audit.log` (timestamp, session, tool, args summary, level,
  outcome `auto` / `approved` / `denied` / `timeout` / `high_denied` /
  `needs_approval`, result summary) — separate from the SQLite session
  DB, greppable at a glance, not committed to git.
- Honest limit, documented: the classifier is heuristic, not formal
  verification — a model can rephrase its way around the blocklist.
  The real backstop is still the Docker container plus the workspace
  sandbox; approval is one more layer, not the only one.
- Verified: pytest 29 passed (13 existing + 16 new approval tests:
  classifier, approval store, timeout, tool-layer guards, audit log,
  non-streaming denial, approval endpoint); mock-LLM end-to-end 18
  checks passed (approve writes the file and the conversation continues,
  deny changes the plan without error, high-risk gets no card, `pytest`
  runs automatically, `MEMORY.md` write goes through a card, audit log
  covers all four outcomes, non-streaming high-risk denied).

## v0.4.0 — 2026-10-26

Long-term memory: a single `agent_workspace/MEMORY.md` file, injected as a
second system message on every request.

- New `backend/memory.py` (stdlib only, no new dependencies): reads
  `agent_workspace/MEMORY.md` fresh on every request (no cache, so an edit
  takes effect on the next message) and injects it after `SYSTEM_PROMPT`,
  before history — `[system, memory, *history, user]`. Two-tier memory:
  SQLite holds the short-term session (last 20 rounds), `MEMORY.md` holds
  long-term preferences. Injection is capped at ~2000 characters
  (`MEMORY_MAX_CHARS`, default 2000); a missing or empty file means no
  injection and no error, behaving exactly like v0.3.
- Manual edit only: the agent does not auto-write memory. `write_file`
  targeting `MEMORY.md` is refused by the tool layer ("please edit it
  manually"), so a mistaken model write cannot pollute every future
  conversation. Auto-write stays closed until the approval-tier version.
- `MEMORY.md` ships with a starter file (Chinese replies, FastAPI +
  Zhipu GLM-4.6) and is tracked in git (`.gitignore` now excepts it —
  the old `agent_workspace/*` rule would have silently ignored it).
- Verified: pytest 13 passed (7 existing + 6 new memory tests: missing /
  empty file, injection content, truncation at the cap, the write guard,
  and the `[system, memory, *history, user]` order); mock-LLM
  end-to-end checks passed (streaming and non-streaming both inject the
  memory as the second system message; deleting the file restores
  v0.3 behavior with no memory message; the write guard holds live).

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
