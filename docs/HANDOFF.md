# Handoff

## Current checkpoint — all 8 stages done (2026-11-08)

Update: the user asked how to showcase every PDF requirement and for a demo script. Rehearsing the demo live with Codex found BUG-011 (date correction flipped intent to reschedule; fixed, S38). Recording-day caveat: "this week, mornings only" has no slots late on a Sunday (correct per DECISIONS §8); the script covers it.

The user asked to continue through stages 7 and 8 without waiting. All 8 stages are done; 413 offline tests + 2 live Ollama tests pass; the generated eval table has 37/37 scenarios. Stage 8: local HTTP API (`errand_agent/api/`), stdlib-only CLI (`errand_agent/cli.py`), `errand_agent/serve.py` + `scripts/serve.sh`, shared `scripts/with_model.sh`, `docs/DEMO.md`, transcript exporter. A live run with Codex through the CLI found BUG-010 (restated default discarded the card) and the reconnect/receipt gap (fixed with `GET /sessions/{id}/card`). 10 bugs logged; three highlighted.

Left for the user: record the 3–5 minute demo following docs/DEMO.md; review `artifacts/transcript/*.md` (git-ignored; contains the user's name in places) and include it; optionally `modelmux login claude` then `scripts/smoke.sh claude`.

Running things from this shell: Docker via `wsl.exe --distribution Ubuntu --user laptop --cd <repo> --exec bash -c '...'`; zsh here doesn't word-split variables (use functions). Never read or print the ModelMux key. Commits: no AI attribution lines.

## Earlier checkpoint — stages 1–3 done and reviewed; pre-stage-4 requests handled (2026-11-07)

User follow-ups after the stage-3 checkpoint:
- Git identity is now `smit153` (set by the user) and history has no AI attribution. **Never add Co-Authored-By or "Generated with" lines to commits.**
- Write-ahead `tool.attempt_started` event added before every tool call (`errand_agent/tools/client.py`); completion `tool.call` shares its `call_id`.
- README.md added (setup, mirror and hash verification). Open items (per-operation model-call/time limits → stage 4; retry worker → stage 6) are in PROGRESS.md.
- `docs/DESIGN_REVISION_v2.md` saved from the user's paste and cross-checked: nothing from v2 is missing in DECISIONS.md §§1–16 / DESIGN.md. Its "no code / stop for approval" step is historical (already done; the build was then approved). v2 calls the PDF "ASSIGNMENT.md"; no separate ASSIGNMENT.md file exists. Follow-ups are in PROGRESS.md Open items. Next: wait for the user's go-ahead on stage 4.

## Previous checkpoint — stages 1–3 done; stopped for user review (2026-11-07)

User approved the build ("Build the Errand Agent") with engineering standards: Python 3.12, mypy --strict, ruff, Pydantic at boundaries, invariant tests first for safety-critical code, parameterized SQL, short transactions, migrations, env config, pinned deps, untrusted model/tool text, explicit error paths, real bugs logged in docs/BUGS.md with regression tests. **Ask before deviating from the design. Stop after stage 3 and show test results.**

Read first: PROGRESS.md, docs/DESIGN.md, docs/DECISIONS.md §§1–16 (§15 build plan). PDF is the source of truth; ASSIGNMENT.md and DESIGN_REVISION_v2.md do not exist.

Done: stage 1 (see PROGRESS.md). Code layout: `errand_agent/{config,clock,errors,trace,claims}.py`, `errand_agent/db/` (Database, session_txn, migrate, migrations/0001_init.sql), `tests/` (conftest recreates `errand_test` once per session and truncates per test).

Done: stage 2a — `errand_agent/tools/{models,errors,context,client}.py`, `errand_agent/mocks/{catalog,backend}.py` (read-only tools). Done: 2b (hold/book/status, RNG, replay, fencing; `tests/helpers.py` builds operation/attempt rows directly for backend tests). Done: 2c (cancel 503 phase, reschedule conflict). Stage 2 complete. Done: 3a — `errand_agent/workflow/{states,snapshots,templates,approval}.py`. Done: 3b — `errand_agent/workflow/{budget,executor}.py`, migration 0002, docs/BUGS.md (3 real bugs). Done: 3c — `errand_agent/workflow/recovery.py`, `tests/test_recovery.py`. **Stage 3 complete: 137 tests pass. Stopped at the user's checkpoint; do not start stage 4 until the user approves.**

Key implementation decisions (within the design):
- Consequential mock calls carry an internal `Fence` (holder, generation, operation_id, attempt_id) in `ToolContext`, never as tool arguments; backend re-verifies claim + reserved attempt + snapshot under the session advisory lock in the same transaction as the effect.
- Lock order: session advisory lock → mock-global lock → rows.
- Claim verification requires an unexpired lease (expired-but-not-taken-over is stale → conservative).

How to run: `docker compose up -d --wait postgres` then `scripts/dev.sh scripts/check.sh`. From this agent's sandbox, wrap with `wsl.exe --distribution Ubuntu --user laptop --cd <repo> --exec bash -c '...'` (fresh WSL process has docker group). Re-lock deps: `scripts/dev.sh scripts/lock.sh` then `python3 -I scripts/verify_lock_hashes.py requirements.lock requirements-dev.lock` (must report 0 failures before installing).

Next (after approval): stage 4 — G1–G11 + scripted model (native-call adapter with state-scoped schemas/enums, deterministic date/number parsing and ranking, grounding scanner, five hallucination attacks, injection, repair/retries, degraded menus), then the real Qwen 3.5 smoke. Known gaps carried forward: per-operation active-time and model-call lifetime counters exist in schema but are not yet charged (model calls arrive in stage 4); job rows are written but no worker consumes them yet (stage 6). Then STOP and show the user test results.

<details>
<summary>Design-phase handoff history (superseded)</summary>

# Handoff

## Current checkpoint — Design Revision v2 complete, approval pending

User explicitly requested documentation only, then stop for their approval before code. **Do not begin implementation or infrastructure changes without that approval.** Read current docs/DESIGN.md and docs/DECISIONS.md §§1–16; appendices and the history below are superseded where conflicting. PDF source is Orange_Engineering_Assignment.pdf; ASSIGNMENT.md does not currently exist. No PDF requirement is waived or reinterpreted.

Completed: 513-word compact design note; full current decision log; G1–G11 guardrail table; E01–E45 planned eval/acceptance families; full PDF requirements→design→test table; eight-stage risk-ordered build plan; supersession table; verbatim v1 decisions and prior compact note archived in DECISIONS.md. All requested files updated. No app code or tests written.

Current design: thin CLI over HTTP to local FastAPI/workflow; single language agent, stateless deterministic workers; local Ollama Qwen 3.5 native tools, initially evaluate qwen3.5:4b but confirm exact tag/size and pin full digest only after the three PDF example smoke tests. Code owns facts/dates/ranking/consequential rendering/approval/execution/status; scoped enums, grounding once-regenerate/template, snapshot match and forced timeout status. Conservative repair + three retries, then visible degraded menu without bypassing the model/approval gate. Retain both token and revision-bound yes.

Persistence: PostgreSQL 16 + pgvector in Compose, same backend for tests. All workflow, provider/replay, memory/vector, queue/outbox and trace state in this DB. Short per-session advisory-lock transactions; no transaction held over model/tool calls. Durable per-session claims/generation checks and backend mutation fencing are required because transaction advisory locks alone cannot protect work outside transactions. SKIP LOCKED jobs handle dispatch/retries/reconciliation/follow-ups. Stable operation keys, separate attempt IDs, lifetime budgets, atomic effect/replay, same-key durable cancel-503 phase and outbox dedupe survive restarts.

Embedding choice: local Ollama nomic-embed-text:v1.5, vector(768), retrieval prefixes; installed digest/dimension verification pending setup. SQL eligibility filters before exact cosine ranking; fact/vector same-row atomic lifecycle. Offline evals use fixture vectors and scripted native calls, with separate local embedding/chat integration tests. No sentence-transformers/PyTorch/SQLite/cloud runtime. Long seeded conversation proves no raw history reaches context; same electrician uses list_bookings.

Call count: one-clarification booking has 9 logical calls across runs 3/5/1, 10 with history (busiest run 6). Physical retry upper bounds 34/38 across conversation, with any tighter tool/time cap able to stop earlier. Retain run caps 10 logical, 40 physical, 20 tool invocations (including internal/forced/retry), 300 active seconds. Keep operation lifetime 3 consequential dispatches, 6 reconciliation calls, 40 physical model calls, 300 active seconds across restarts. Input/output 4096/768, allocation/drop order retained, Qwen tokenizer/template validation pending.

Validation performed: reread PDF; checked all guardrail/scenario IDs, eight build stages, allocation sum and verbatim archive. Verified proposed tool/embed interfaces and model family in official sources linked in decisions. No real model performance, DB tests or application outcomes claimed. One-page export check remains unverified: sandbox denied Chromium launch and installed groff lacks PDF device. Do not claim pagination passed; compact note is 513 words and needs final one-page check after actual trace excerpt. No approval escalation was requested. Old DESIGN.mdx/HTML remains superseded; don't present it as v2.

Next action: wait for user approval. If approved, follow DECISIONS.md §15 from stage 1; do not skip safety gates. Maintain progress/handoff after each small task. Required raw timeout-after-success trace, generated eval table, three actually encountered bugs/regressions, clean repo/README, transcript and 3–5 minute side-by-side demo are still future deliverables.

<details>
<summary>Historical setup and superseded handoffs</summary>

# Handoff

## Active revision v2 — documentation only

User supplied Design Revision v2; it overrides earlier choices. In progress: PostgreSQL 16 + pgvector, FastAPI/thin CLI, native Ollama Qwen 3.5 tool calls, G1–G11, local Ollama embeddings, durable jobs/outbox, full PDF traceability and eight-stage risk-ordered build plan. Preserve superseded decisions. Do not write application code. On completion stop for the user’s explicit approval.


## Latest design review — 2026-11-07

The user answered eleven design-review questions and explicitly requested documentation only. Updated docs/DESIGN.md and created docs/DECISIONS.md with all eleven numbered decisions, alternatives, reasoning, and planned acceptance checks. Local Ollama qwen3:4b replaces ModelMux as the assignment inference path; ModelMux setup is separate infrastructure. One agent plus deterministic watcher; stable operation keys with distinct attempts; revision-bound token/yes confirmation; conservative JSON repair plus three generation retries; ten logical/forty physical model calls per run; 4,096 input tokens; local MiniLM retrieval with fixture vectors and encoder integration checks; SQLite short transactions and singleton worker; durable same-key cancellation retries. Full flow count is seven decisions, eight with history, across approval-separated runs. Artifact pins, latency, exact token usage and all tests remain future implementation work. No code was written. The MDX/HTML companion is superseded and needs regeneration before being presented as current. Earlier setup/history below is historical and can conflict with this latest review; this section takes precedence.

## Current work (historical setup)

ACTIVE TASK (2026-11-07): User selected https://github.com/smit153/modelmux/ (their own project) and explicitly requested setup/run, possibly installing Docker in WSL. This authorizes infrastructure setup, while Errand Agent application coding remains paused. Cloned upstream HEAD `5fcddd1054cf7dc361d4e4b36547fe17146cf4fd` to ignored `.local/modelmux`; no applicable AGENTS.md found there. Read README, docs/SETUP.md, docs/CLI.md. Host is Ubuntu 26.04 WSL2, systemd running, ~11 GiB RAM, ~950 GiB available disk. Docker/Docker Desktop were not found. `sudo -n true` indicates authentication is needed; checking supported WSL/rootless administration options. An async question is pending: Codex, Claude Code, or both providers. Do not assume the preselected answer is approval. ModelMux provides Chat Completions and simulated tool_calls, not Responses API; model inference still uses hosted provider subscriptions/accounts, not local model weights.

User answered: **"i will set it up for now just spin up both continer"**. Prepare/start both containers; leave provider authentication to the user. ModelMux intentionally exits startup if no provider login is present, so do not claim API readiness until logged in. `modelmux up` prepares both services but starts only logged-in providers; a direct Compose start can show the expected unauthenticated startup failure. ModelMux CLI 0.3.1 is installed globally. WSL's supported root invocation (`wsl.exe --distribution Ubuntu --user root --exec ...`) works; Docker CE/Compose installation is running via `.local/install-docker.sh`, using Docker's official Ubuntu apt repository. Script also adds user `laptop` to the docker group. Existing shells need `sg docker -c ...` or a new group session afterward.

Docker installation is complete: Engine 29.9.3, Compose 5.7.0, daemon enabled/running, `laptop` in docker group. Running `sg docker -c 'docker run --rm hello-world && modelmux up'` to verify runtime and prepare both providers. Provider login remains explicitly deferred to the user.

Correction: `sg`/`newgrp` are absent here. Fresh `wsl.exe --distribution Ubuntu --user laptop --exec ...` processes correctly pick up docker group membership. Docker `hello-world` passed. ModelMux preparation is running in exec session 57452, invoked with the absolute Node executable and global ModelMux `dist/bin.js`; it generated a private gateway key and is pulling the published pinned image. `docs/MODELMUX.md` contains the user-facing setup/login/operations guide. Design Markdown and MDX now use ModelMux, and HTML validation/render are in progress. No login initiated.

Hardware follow-up: User asks what specs Ollama needs. Official sources checked: https://ollama.com/library/qwen3:4b (2.5 GB Q4_K_M), https://ollama.com/library/qwen3:8b (5.2 GB Q4_K_M), https://docs.ollama.com/gpu, https://docs.ollama.com/context-length, https://docs.ollama.com/faq. Guidance: 8 GB system RAM can be an entry point for a 4B quantized model; 16 GB is a more practical target; an 8 GB supported GPU is a useful target for an 8B model at short context. These are planning estimates; available memory, context, and concurrency matter. CPU-only is supported but slower. No hardware inspection, model install, or live benchmark performed; no design files changed.

Latest design discussion (2026-11-07): User questions Ollama and asks for free alternatives and approximate paid API cost. Researched official Google Gemini and OpenAI pricing. Cost illustration assumes 10,000 total input + 2,000 billable output tokens across a full errand conversation. At listed standard rates: Gemini 3.1 Flash-Lite ($0.25/$1.50 per million input/output) costs $0.0055 per conversation; GPT-6 Luna ($0.10/$0.50) costs $0.002; GPT-6.1 Terra ($1/$5) costs $0.02. Gemini lists a free tier subject to quotas. These are estimates, not measured usage or model-quality results. All scripted evaluations remain offline. No provider is chosen, no paid calls made, and design sources/HTML are unchanged pending that decision. Sources: https://ai.google.dev/gemini-api/docs/pricing and https://developers.openai.com/api/docs/pricing.

The PDF's local/no-external-integration wording needs an explicit design interpretation before making a hosted LLM the only option. Hosted inference may be intended to be allowed while errand providers/payments are mocked, but that is an inference, not an explicit PDF exception.

Update 2026-11-07: Completed the requested Visual Plan design document after write/network access was enabled. Source: `docs/DESIGN.mdx`; viewable standalone output: `docs/DESIGN.plan.html` (about 1.9 MB). Installed vplan 0.24.0 globally, followed the remote skill and static-export instructions, validated with `vplan check docs/DESIGN.mdx`, and rendered with `vplan --static --no-diff docs/DESIGN.mdx`. This is documentation only; application implementation remains paused.

Browser verification passed offline using Playwright: six diagrams rendered, zero JavaScript errors, zero external asset requests, no page overflow at 1440px and 390px widths. Visually inspected the desktop screenshot. An initial mobile table overflow was fixed in the MDX, then validation/render/browser checks were rerun successfully. Screenshots are at `/tmp/errand-design-desktop.png` and `/tmp/errand-design-mobile.png`. These are document checks, not application requirement tests. Baseline pytest still collects no tests (exit 5).

The user initially requested the complete Errand Agent from Orange_Engineering_Assignment.pdf, with a design shown before implementation and continuously updated progress/handoff files. Their latest instruction is **“no code for now just the deisgn”**. Respect that instruction: implementation is paused. No application code has been written.

Read all three PDF pages with `.venv/bin/python` and `pypdf`. `pdftotext` is unavailable. Created PROGRESS.md because none existed. Ran `.venv/bin/python -m pytest -q`: no tests ran, exit code 5. No applicable AGENTS.md was found. Saved the proposed design to docs/DESIGN.md and presented the approach in chat.

Architecture proposed: Python terminal chat; one agent; real local Ollama LLM plus scripted offline adapter; explicit persisted state machine; SQLite operations and mock backend; exact-action approval tokens enforced outside the model; durable idempotency and authoritative timeout reconciliation; bounded execution; provenance-aware memory and capped context; proactive background cancellation watcher; structured traces and generated eval reports.

## Next 3 steps

1. Finish Docker CE/Compose installation (exec session 43123) and verify the daemon and container runtime.
2. Use ModelMux CLI to pull its pinned image and prepare both services, then start both containers. Leave login to the user and report any expected auth-related exit accurately.
3. Update the design sources/HTML for ModelMux, document the two login commands and service URLs, and update tracking. Keep application implementation paused.

## Environment and remaining constraints

- Repository root: `/home/laptop/gen-ai/assignment/orange`.
- `.venv` includes pypdf and pytest; no application dependencies have been installed.
- `ollama` and a desktop browser were not found on PATH. A real model run and screen recording still need environment setup if implementation resumes.
- Preview-only tooling: Playwright core comes with global vplan at `/home/laptop/.nvm/versions/node/v26.13.0/lib/node_modules/vplan/node_modules/playwright-core`. Headless Chromium and FFmpeg were downloaded to `~/.cache/ms-playwright`. Missing shared libraries were downloaded and unpacked under `/tmp/errand-preview-libs/root` without changing system packages; browser checks used `LD_LIBRARY_PATH=/tmp/errand-preview-libs/root/usr/lib/x86_64-linux-gnu`.
- `.git` was empty/read-only at initial inspection. Permissions are now unrestricted, but no Git repository delivery was attempted for this documentation task.
- Ollama's official chat API was checked at https://docs.ollama.com/api/chat; it supports POST /api/chat, messages, structured output, and usage counts. No live model was exercised.
- No generated eval results, real bug reports, or execution traces exist. Do not fabricate these deliverables.
- The original five-extra-features request is gated on all PDF requirements passing; do not propose them prematurely.

</details>


</details>
