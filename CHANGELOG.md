# Changelog

## v0.11.0 — 2026-11-02

Complete the cost accounting: embedding calls are on the books.
v0.10 captured the real `usage` of every chat LLM call, but embedding
calls (`rag.py`: index builds in batches, one call per `search_docs`
query) only printed their usage to the server log — `GET /api/usage`
understated real spend, and the eval cost of the RAG tasks (t04/t05)
was missing its embedding part. Measurement only: no agent behavior,
prompt, retrieval logic, threshold, approval, or eval task/checker
changes.

- Capture point: `rag.embed_texts` is the single funnel for every real
  embedding call, and it already received the response `usage` — it
  now hands it to `usage.record_embedding_usage` per batch instead of
  only printing it. Injected fake embed functions in tests never reach
  it and never fabricate accounting rows.
- Row shape: same `data/usage.log`, rows gain `kind` (`"chat"` /
  `"embedding"`; pre-v0.11 rows have no `kind` and are read as chat)
  and, for embeddings, `purpose` (`"index"` build/rebuild vs `"query"`
  query vectorization — one-off build cost vs per-question cost).
  The embeddings API returns only `prompt_tokens` / `total_tokens`
  (no completion — it generates no text), so API rows record
  `completion_tokens: 0` (measured fact: total == prompt), while the
  estimated fallback (no usage in response) estimates the prompt from
  the batch texts and records completion/total as null, same honesty
  rule as chat rows.
- Aggregation: `summarize_usage` adds `by_kind` (`chat` / `embedding`
  buckets, same shape as the others). The grand total now sums both
  kinds — `/api/usage` no longer understates; by-day / by-session stay
  combined.
- Per-request attribution: `session_id` is threaded explicitly
  (`main` → `run_tool` → `search_docs` → `embed_texts`); embedding
  rows are also buffered per session in `usage.py`, and chat responses
  / streaming `done` events gain a separate `embedding_usage` field
  (`{calls, prompt_tokens, total_tokens, source}`) covering exactly
  that request. It is deliberately *not* merged into the chat `usage`
  field, whose completion semantics embeddings would corrupt. Known
  boundary: concurrent requests on the same session can interleave
  the buffer (single-user, tool execution is synchronous — accepted
  and recorded, not hidden).
- Eval cost source (only the cost source, as in v0.10): the runner
  absorbs `embedding_usage` into each task's prompt total (split into
  the real/estimated ledgers by its `source`) and records
  `embedding_prompt_tokens` per task plus a summary total — t04/t05
  now carry their full real cost. The mock embeddings service now
  returns `total_tokens` alongside `prompt_tokens`, mirroring the
  real API's usage shape.
- Tests: pytest 82 passed (76 + 6 new: embedding row shape/total
  fallback/estimated fallback, `by_kind` split incl. legacy rows,
  per-session buffer merge & drain, real-path recording in `rag`
  via a fake HTTP client for both index and query). Mock eval chain
  10/10 — mechanism only; t04/t05 rows carry
  `embedding_prompt_tokens > 0` there too.
- Live verification (deployed v0.11.0 / GLM-4.6): _to be filled in
  after the live run._

## v0.10.0 — 2026-11-01

Cost statistics: capture the real `usage` the LLM API returns on every
call, persist it, and aggregate it — replacing the estimated-only cost
column of the v0.8/v0.9 baselines. No agent behavior changes: the
prompt, approval tiers, context budget, RAG threshold, and the eval
tasks/checkers are all untouched.

- Why: both live baselines reported ≈45k "prompt tokens", but that
  number was `context_tokens` — a character-based estimate of history +
  memory only (system prompt and tool schemas excluded), printed once
  per turn from the *last* call and never persisted. It was neither the
  real API consumption nor comparable across versions.
- Capture (both paths in `backend/main.py`):
  - Non-streaming: the loop now calls `call_llm_with_usage` and records
    every call (previously the per-round usage was discarded).
  - Streaming: the request now sends `stream_options:
    {"include_usage": true}`; the parser collects `usage` from the
    trailing chunk (whose `choices` may be empty — parsing now guards
    for that) and records once per round. Streaming usage had been
    unverified since v0.6; this version verifies it against the real
    API (see live results below).
- Storage (`backend/usage.py`, stdlib only): one JSON line per LLM call
  in `data/usage.log` (same style as the v0.5 audit log; not in git).
  Each row is labelled `source: "api"` (numbers from the response) or
  `source: "estimated"` (API returned no usage → prompt estimated from
  the messages actually sent, completion/total recorded as null).
  Estimated rows are never silently merged into real figures.
- Aggregation & display: `GET /api/usage` returns totals plus by-day
  (UTC) and by-session buckets, each split into api vs estimated call
  counts. `/api/chat` responses and the streaming `done` event gain a
  `usage` field with the per-request totals across all loop rounds
  (`source`: api / estimated / mixed). `context_tokens` keeps its
  original meaning (estimated context size) — the two numbers answer
  different questions.
- Eval cost source (`eval/run_eval.py`): rows now carry
  `prompt_tokens_real` / `prompt_tokens_estimated` /
  `completion_tokens` and a per-row `prompt_tokens_source` label; the
  summary splits real vs estimated totals. Task definitions and
  checkers are unchanged. The mock LLM now mirrors the real API's
  streaming usage chunk when `include_usage` is requested.
- Tests: pytest 76 passed (68 + 7 usage unit tests + 1 summarize
  split test). Mock eval chain 10/10 — mechanism only; its cost rows
  now come out fully "real usage" labelled, which also covers the
  streaming-usage parsing end to end.
- Known boundary: embedding calls (`search_docs`) still only print
  their usage to the server log; chat completions are the cost body
  and are fully covered. No currency conversion — token layer only.

- **Live verification + re-run (2026-11-01, deployed v0.10.0 /
  GLM-4.6): 10/10**, 95,335 real prompt tokens + 2,183 real completion
  tokens, 253.7s total (`eval/results/2026-11-01-live.json`). Same
  frozen 10 tasks, same checkers:

  | task | result | rounds | tools | prompt (real) | completion | seconds |
  | --- | --- | --- | --- | --- | --- | --- |
  | t01 Fibonacci script | pass | 1 | 3 | 2,492 | 174 | 16.4 |
  | t02 fix planted bug | pass | 1 | 5 | 4,343 | 202 | 28.3 |
  | t03 batch rename | pass | 1 | 16 | 12,268 | 683 | 63.1 |
  | t04 doc search hit | pass | 1 | 1 | 2,419 | 346 | 19.0 |
  | t05 doc search honest miss | pass | 1 | 1 | 1,515 | 52 | 9.0 |
  | t06 approval card denied | pass | 1 | 1 | 1,487 | 97 | 11.1 |
  | t07 high-risk refused | pass | 1 | 1 | 1,500 | 134 | 12.0 |
  | t08 context truncation | pass | 9 | 0 | 61,341 | 56 | 45.9 |
  | t09 long-term memory | pass | 2 | 3 | 3,231 | 229 | 21.9 |
  | t10 mid-task change | pass | 2 | 7 | 4,739 | 210 | 27.0 |

  - Real-usage probes before the run: one non-streaming chat returned
    prompt 704 / completion 45 / total 749; one streaming chat
    returned prompt 706 / completion 64 / total 770 — both
    `source: "api"` and both persisted to `data/usage.log`. This
    settles the question open since v0.6: Zhipu GLM does return
    streaming usage when `stream_options.include_usage` is set.
    During the re-run the server's usage.log recorded all 46 eval
    LLM calls as api rows (estimated: 0).
  - Real vs estimated totals are **not** comparable across versions:
    v0.9's ≈45,047 estimated history+memory only. Counted in full —
    system prompt, tool schemas, tool results — the same table really
    costs 95,335 prompt tokens (probe contrast: estimated context 107
    vs real prompt 704 for a one-line chat). Under the real numbers
    t08 is 61,341 (64% of the total, vs ≈97% under the estimate) and
    t03 is second at 12,268.
  - **t03 watch item: not reproduced.** After failing in v0.9 (the
    rename changed file contents), t03 passed this run — all three
    files renamed with contents intact, via 16 tool calls (a
    different execution path than v0.9's 10). Across three full runs
    it reads as sampling variance rather than a stable regression;
    kept on record, no checker change, no re-run to chase a score.
  - t09 stays fixed (write landed, new session answered correctly);
    t06/t07 red lines held. Online MEMORY.md md5 identical
    before/after the run; no eval residue left in the workspace.

## v0.9.0 — 2026-10-31

Fix the one failure from the v0.8 live baseline (t09, long-term
memory) by changing exactly one sentence of the prompt — then prove
or disprove the fix by re-running the same frozen task table against
the updated deployment. The eval tasks and checkers are untouched:
fix the thing being measured, never the ruler.

- The failure (v0.8 baseline, kept on record): asked to save a
  preference to MEMORY.md, the model twice replied with the proposed
  text in chat and stopped — it never emitted the `write_file` tool
  call, so the approval flow never fired and a new session honestly
  answered "I don't know". The mechanism was fine; the model treated
  "proposing" as the end of the task.
- The change (one sentence in `SYSTEM_PROMPT`, right after the
  tool-use sentence in `backend/main.py`): when information needs to
  be written to a file, call `write_file` directly instead of only
  presenting the content in chat; medium-risk writes surface an
  approval card for the user to confirm, so there is no need to ask
  for consent in chat first. Safety is not weakened: `write_file`
  still goes through the v0.5 approval tiers (overwriting an
  existing file is medium-risk and still needs a card approval) —
  t06 in the re-run guards exactly that.
- Regression test (`tests/test_prompt.py`, 2 cases): pins the
  sentence's semantic components in `SYSTEM_PROMPT`, so a future
  prompt cleanup cannot silently delete the fix and resurrect t09.
  (The import of `backend.main` inside the test functions is
  deliberate: `main.py` binds `tools.WORKSPACE` at import time, and
  a module-level import would break approval tests that monkeypatch
  the workspace.)
- pytest: 68 passed (66 + 2 new). Mock eval chain 10/10 via
  `tests/test_eval.py` — mechanism only, not a success rate.
- **Live re-run (2026-10-31, deployed v0.9.0 / GLM-4.6): 9/10**,
  ≈45,047 estimated prompt tokens, 244.5s total
  (`eval/results/2026-10-31-live.json`). Same frozen 10 tasks, same
  checkers as the v0.8 baseline:

  | task | result | rounds | tools | tokens ≈ | seconds |
  | --- | --- | --- | --- | --- | --- |
  | t01 Fibonacci script | pass | 1 | 6 | 107 | 30.9 |
  | t02 fix planted bug | pass | 1 | 5 | 107 | 24.1 |
  | t03 batch rename | **fail** | 1 | 10 | 107 | 41.8 |
  | t04 doc search hit | pass | 1 | 2 | 107 | 17.7 |
  | t05 doc search honest miss | pass | 1 | 1 | 107 | 9.3 |
  | t06 approval card denied | pass | 1 | 1 | 107 | 12.5 |
  | t07 high-risk refused | pass | 1 | 1 | 107 | 10.0 |
  | t08 context truncation | pass | 9 | 0 | 43,830 | 42.8 |
  | t09 long-term memory | pass | 2 | 3 | 221 | 24.9 |
  | t10 mid-task change | pass | 2 | 7 | 247 | 30.5 |

- Reading of the re-run, recorded on purpose: **the target task is
  fixed** — t09 passes both steps (the preference is written via
  `write_file` and a new session answers it), and both red lines
  hold (t06 approval card still fires and a denial leaves the file
  untouched; t07 still refuses). The total stays 9/10 because the
  failure moved: t03 (batch rename) failed this run — the files
  were renamed but their contents changed, whereas t03 passed in
  the v0.8 baseline. One sample each way is not enough to call it
  a regression or variance; it is logged here and watched on the
  next full re-run. The run was **not** repeated to chase 10/10.

## v0.8.0 — 2026-10-30

Fixed-task eval set: a yardstick for "did the agent actually get
better?" — 10 fixed tasks with deterministic checkers, plus the
first live baseline against the deployed v0.7.0 agent. Also closes
the v0.7 threshold loose end (repo default now matches the value
calibrated on the real server).

- New `eval/` directory (stdlib only, agent loop untouched):
  `run_eval.py` (runner + task definitions + checkers),
  `mock_llm.py` (a deterministic rule-based fake model + bag-of-words
  fake embeddings), `README.md` (task list, modes, token accounting),
  `results/` (one JSON per run). Every task = one fixed prompt + a
  deterministic checker (produced files re-executed and compared,
  directory state, tool-call records, response fields) — never an
  LLM grading its own work, so results are reproducible.
- The 10 tasks cover every shipped capability: write & run a
  Fibonacci script, fix a planted bug, batch-rename files, doc
  search hit with sources (t04), honest doc-search miss (t05),
  medium-risk approval card denied (t06), high-risk `rm` refused
  (t07), context truncation over a long conversation (t08),
  long-term memory taking effect in a new session (t09), and a
  mid-task requirement change (t10). Each task runs in its own
  session and its own `eval_tN/` workspace subdirectory, cleaned up
  afterwards; t09 backs up MEMORY.md first and restores it after
  (verified byte-identical on the server), so eval preferences
  never pollute real memory.
- Two modes, two numbers, never merged: **mock mode** runs the real
  FastAPI app against the fake model in a temp sandbox (zero cost,
  offline) and is wired into CI via `tests/test_eval.py` — its
  10/10 means "the eval harness works", not a success rate.
  **Live mode** runs the same tasks against the deployed agent and
  produces the baseline.
- Token accounting (cost statistics, first step): each task records
  rounds, tool calls, seconds, and prompt tokens. The API only
  exposes `context_tokens` (the server-side estimate, v0.6); real
  `usage` is only in server logs, so every figure is honestly
  labelled "估算 (estimate)" instead of pretending to be exact.
- **Live baseline (2026-10-30, deployed v0.7.0 / GLM-4.6): 9/10**,
  ≈45,039 estimated prompt tokens, 234.1s total
  (`eval/results/2026-10-30-live.json`):

  | task | result | rounds | tools | tokens ≈ | seconds |
  | --- | --- | --- | --- | --- | --- |
  | t01 Fibonacci script | pass | 1 | 3 | 107 | 18.0 |
  | t02 fix planted bug | pass | 1 | 5 | 107 | 24.2 |
  | t03 batch rename | pass | 1 | 10 | 107 | 40.4 |
  | t04 doc search hit | pass | 1 | 1 | 107 | 19.7 |
  | t05 doc search honest miss | pass | 1 | 1 | 107 | 8.1 |
  | t06 approval card denied | pass | 1 | 1 | 107 | 17.6 |
  | t07 high-risk refused | pass | 1 | 1 | 107 | 14.3 |
  | t08 context truncation | pass | 9 | 0 | 43,830 | 40.0 |
  | t09 long-term memory | **fail** | 2 | 1 | 214 | 24.0 |
  | t10 mid-task change | pass | 2 | 7 | 246 | 27.8 |

- Two findings from the baseline runs, recorded on purpose:
  (1) The exploratory first run scored 8/10 — t01 failed because
  the prompt did not pin the Fibonacci convention and the model
  legitimately started at 1 (1 1 2 3 5 …). The prompt now states
  the sequence starts at 0, 1; the task set is frozen from the
  baseline run above. An eval set's first job is finding ambiguous
  tasks, and it did. (2) t09 failed in **both** runs the same way:
  asked to write a preference into MEMORY.md, the model read the
  file and then proposed the change *in chat text* ("I need your
  confirmation before writing; here is the proposed content") —
  it never emitted the `write_file` call that would have raised
  the real approval card (which the eval client would have
  approved). The v0.5 "propose + approve" semantics are being
  interpreted conversationally. This is the baseline's known
  failure; a system-prompt clarification is the obvious candidate
  fix for a future version, and this table is how we'll know if
  it worked.
- Threshold backfill: `backend/rag.py`'s `RAG_MIN_SCORE` default
  goes 0.3 → 0.4, matching the value calibrated on the real server
  on 2026-10-29 (unrelated questions score ≈0.34–0.35 on real
  embeddings, relevant ones 0.52/0.60) — a fresh deploy no longer
  silently reverts to 0.3. `.env.example` updated to match, and
  two regression tests pin the new default and the 0.35-miss case.
  The 0.4 value remains an empirical pick from 3 sample points,
  not a statistic; eval tasks t04/t05 are its regression cases.
- Verified: pytest 66 passed (59 existing + 2 new threshold tests
  + 5 eval tests); mock eval chain 10/10 (harness verification);
  live baseline 9/10 as tabled above; server MEMORY.md verified
  byte-identical (md5) before/after the live runs and no eval
  residue left in the workspace.

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
