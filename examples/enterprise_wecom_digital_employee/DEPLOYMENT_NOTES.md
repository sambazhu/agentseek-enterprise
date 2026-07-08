# Enterprise WeCom Digital Employee — Deployment Notes (Mac mini)

Handoff notes from deploying/verifying this example on a company Mac mini
(branch `enterprise/wecom-runtime`, then integration branch
`enterprise/wecom-runtime-v0.0.4`, 2026-06-27 through 2026-06-29). Covers the
DM-connection root cause, the upstream integration context, the working
configuration, known-issue workarounds, and the production-ready operating
state.

## v0.0.7 candidate: PostgreSQL auth + pgvector semantic backend (pending Mac mini verification)

Branch: `enterprise/v0.0.7-pgvector`.

Goal:

- keep short-term memory and explicit durable employee memory on PostgreSQL via
  `AGENTSEEK_ENTERPRISE_MEMORY_SQLALCHEMY_URL` and
  `AGENTSEEK_ENTERPRISE_STORE_SQLALCHEMY_URL`;
- move ContextSeek semantic memory from local SeekDB to PostgreSQL + pgvector
  with `AGENTSEEK_CTX_STORAGE_BACKEND=pgvector`;
- use bge-m3 dense embeddings only, exported to ONNX and loaded through
  onnxruntime/tokenizers in the gateway process;
- start with a fresh pgvector semantic table (`contextseek_pgvector_items` by
  default). Do not migrate existing SeekDB data because dimensions/model
  semantics differ.

Expected pgvector schema:

```sql
CREATE TABLE IF NOT EXISTS contextseek_pgvector_items (
    id bigserial PRIMARY KEY,
    scope text NOT NULL,
    content text NOT NULL,
    embedding vector(1024) NOT NULL,
    source text,
    source_type text,
    tags jsonb,
    created_at timestamptz DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_contextseek_pgvector_items_scope
    ON contextseek_pgvector_items (scope);

CREATE INDEX IF NOT EXISTS idx_contextseek_pgvector_items_embedding_hnsw
    ON contextseek_pgvector_items USING hnsw (embedding vector_cosine_ops);
```

Candidate runtime env:

```env
AGENTSEEK_CTX_STORAGE_BACKEND=pgvector
AGENTSEEK_CTX_PGVECTOR_URL=postgresql+psycopg://agentseek_app:<password>@localhost/agentseek
AGENTSEEK_CTX_PGVECTOR_TABLE=contextseek_pgvector_items
AGENTSEEK_CTX_PGVECTOR_DIMS=1024
AGENTSEEK_CTX_BGE_M3_ONNX_MODEL_PATH=./models/bge-m3-onnx/model.onnx
AGENTSEEK_CTX_BGE_M3_TOKENIZER_PATH=./models/bge-m3-onnx/tokenizer.json
```

The pgvector path requires `psycopg[binary]` in the project environment. The
workspace lock was not changed in this candidate because the current repository
lock resolution is constrained by the pinned `bub==0.3.9`; Mac mini should
provide the driver in the deployment venv while verifying this branch.

PostgreSQL auth target:

- create a least-privilege `agentseek_app` login role;
- use SCRAM passwords in the local SQLAlchemy URLs stored only in `.env`;
- switch localhost `pg_hba.conf` rules from `trust` to `scram-sha-256`;
- confirm `psql -U agentseek_app` fails without a password;
- restart the gateway and re-run identity, short-term memory, explicit durable
  memory, pgvector semantic recall, MCP list, and one confirmed MCP tool call.

This candidate preserves the v0.0.6 MCP rollback constraint: do not change
`mcp_policy.py`, `tools.py`, or confirmation behavior.

## Verified working (end-to-end)

WeCom callback → signature verify/decrypt → `open_userid` → plaintext userid
(e.g. `zhuchunlin`) → DM identity lookup (`朱春霖 / 数智产品研发团队 / 团队长兼数据架构师`)
→ model (`glm-5.2` via DashScope) → reply. Confirmed with `我是谁 → 你好，朱春霖！...`
including full org path/role. Final verified state: FlClash in **system-proxy
mode (TUN off)** — see the FlClash section for why TUN must stay off.

## Test log (2026-06-27, Mac mini)

What was tested and the outcome, so the next session knows the current state:

- **End-to-end identity (live, WeCom):** `我是谁 → 你好，朱春霖！` with full
  org path/role. Needs both the DM lookup (employee context) and the WeCom API
  (`openuserid_to_userid`) to succeed. PASS with FlClash TUN off.
- **DM JDBC connect (probe):** `scripts/probe_staff_identity.py --oa zhuchunlin
  --source python-db` returns the full EmployeeContext (name/dept/post/org).
  PASS with FlClash TUN off (or TUN on + the `/32` route).
- **WeCom retry dedup (live):** a slow-reply message produces `1 × msgtype=text`
  + `N × msgtype=stream` (polls); 1 agent turn, 1 reply, no flood.
  `wecom.duplicate_msgid` is not exercised live (WeCom polls, doesn't re-send
  text) — covered by unit tests. See "Verification: WeCom retry dedup". PASS.
- **Unit tests:** `contrib/agentseek-wecom/tests/test_channel.py` — in-flight
  duplicate reuses the stream; after TTL expiry reprocessing is allowed. PASS.
- **FlClash/DM diagnosis:** tcpdump showed 0 packets for Java's DM SYN under
  TUN; full handshake with FlClash quit. DBeaver (system proxy) connected, JDBC
  (raw socket) didn't. Root cause = FlClash TUN interception. Resolved
  (TUN off / `/32` route). PASS.
- **FlClash/WeCom-API diagnosis:** `openuserid_to_userid` returns `60020` via
  the proxy egress (`45.207.34.86`), returns `zhuchunlin` via direct
  (`112.95.215.20`, allowlisted). Resolved (TUN off). PASS.
- **JVM + ONNX crash — FIXED via subprocess isolation (commit 6eb8f4f).**
  Previously SIGBUS (exit 138) when both `libjvm` and `onnxruntime` were
  in-process. The DM JDBC lookup now runs in a child process
  (`AGENTSEEK_IDENTITY_DM_EXECUTION_MODE=subprocess` → `dm_staff_sidecar`), so
  the gateway process loads only `onnxruntime`, never `libjvm`.
  **Verified live on the Mac mini (2026-06-28), FlClash TUN off:**
  1. `probe_staff_identity.py --oa zhuchunlin` (subprocess mode) → full
     EmployeeContext (朱春霖 / 数智产品研发团队). PASS.
  2. Gateway `我是谁` → `你好，朱春霖！` (identity via subprocess + seekdb ONNX
     loaded in-process). No SIGBUS. PASS.
  3. `请长期记住：我偏好简洁的企微回复，最多三条要点。` → stored to seekdb
     (`已长期记住：…`). No crash. PASS.
  4. `我的长期偏好是什么？` → answered correctly. PASS.
  5. **Restart gateway (clear session) → `我的长期偏好是什么？` again → still
     answered correctly** → seekdb persistence confirmed (not session memory).
     PASS.
  Zero SIGBUS / exit 138 across all steps. `STORAGE_BACKEND=seekdb` is now the
  production setting; `memory` is only a rollback if a crash reappears.
- **Employee identity cache (commit f391775) — VERIFIED live (2026-06-28).**
  `AGENTSEEK_ENTERPRISE_IDENTITY_CACHE_ENABLED=true`, TTL 600 s, max 1024
  entries. Only successful EmployeeContext is cached. Verified by monitoring the
  DM sidecar subprocess: 1st `我是谁` → `SIDECAR_SPAWNED` (cache miss → DM
  subprocess ran) → 朱春霖; 2nd `我是谁` (within TTL) → **no new sidecar**
  (cache hit → identity served from gateway memory) → 朱春霖. Zero SIGBUS.
  TTL expiry / no-cache-on-failure are covered by the unit tests
  (`contrib/agentseek-enterprise/tests/test_plugin.py`, 28 passed).
- **Persistent DM sidecar mode (commit 40ffd81) — VERIFIED live (2026-06-28).**
  `AGENTSEEK_IDENTITY_DM_EXECUTION_MODE=sidecar`: a long-lived child process
  holds the DM/JDBC connection; the gateway still never loads `libjvm`;
  parent↔child via stdin/stdout JSON-lines (no network port). Verified by
  watching the sidecar **process pid**: 1st `我是谁` started sidecar pid=97628;
  a 2nd `我是谁` ~3 min later (cache TTL already expired → cache miss) **reused
  the same pid=97628** (started time unchanged, still only 1 sidecar process)
  → identity resolved, no cold JVM start. Zero SIGBUS / exit 138 / stale
  connection. (Cache-hit itself is mode-independent; see the cache entry above.)
  **Minor finding:** the `DM identity sidecar started pid=…` INFO log in
  `dm_staff_provider` did not reach the gateway's loguru output (stdlib `_LOG`
  not routed). Fixed in code by routing enterprise runtime logs through a
  loguru-aware adapter; verify visibility on the next Mac mini pull.
  Mac mini now runs `sidecar` + TTL 600. Roll back to `subprocess` if a crash
  or stale-connection error reappears.
- **`agentseek create` template — VERIFIED end-to-end on a rendered project
  (commit ec3cc20, 2026-06-29).** Rendered a clean standalone project via
  `agentseek create deepagents/enterprise-wecom`, copied in the working `.env`
  + DM jar + `mcp.local.json`, `uv sync`, started with the template's
  `scripts/run_gateway.sh`. Full live sweep, all PASS:
  1. `我是谁` → 朱春霖 (identity via sidecar; sidecar pid stable, no new JVM).
  2. Short-term memory: `帮我记一下…出差` → `我刚才说我要去哪里？` recalled.
  3. Long-term seekdb: `请长期记住：…数据架构` → `我的工作职责是什么？`
     retrieved.
  4. **seekdb persistence**: restart gateway (fresh session) → still recalled.
  5. MCP tools: `列一下当前可用的 MCP 工具` → listed 4 services (incl. Tavily
     search/extract/crawl/map/research).
  Plus: WeCom text-retry dedup fired (`wecom.duplicate_msgid`); zero SIGBUS.
  The template (`run_gateway.sh`, `.env.example` defaults, launchd plist,
  `vendor/.gitkeep`) produces a working project. Note: the first `uv sync` of a
  fresh render needs network (numpy via PyPI, `bub-mcp` via git) — over FlClash's
  proxy, set `git config --global http.version HTTP/1.1` first to avoid git
  HTTP2 stalls.
- **Production preflight + LaunchAgent托管 (commit 7129af1) — VERIFIED
  (2026-06-29).** `scripts/prod_check.py --env-file .env` on the rendered
  project: all checks OK (model/WeCom/DM/JVM-isolation/cache/namespace-secret/
  paths/seekdb/MCP/launchd-plist). `LANGSMITH_TRACING=false` (will move to
  open-source Langfuse later) → preflight passes with **0 warnings**. Then
  installed the template's user LaunchAgent (path-fixed to the project dir):
  `launchctl load` started the gateway via `run_gateway.sh` (RunAtLoad); killing
  the gateway process was auto-recovered by `KeepAlive` in ~2 s (new pid). The
  gateway now runs under launchd (boot-start + crash-restart). Note: the plist
  defaults to `/opt/agentseek/<slug>`; override the cookiecutter
  `deployment_path` (or edit the plist) for other locations.

## Upstream v0.0.4 integration branch (2026-06-29)

Why this branch exists:

- The internal enterprise work was first stabilized on
  `enterprise/wecom-runtime` through commit `dcd06a2`.
- Meanwhile upstream `main` advanced to AgentSeek `v0.0.4` (`12bd58f`), with
  a large lifecycle-toolkit refactor (`e87460d`) that changed CLI/docs/template
  expectations.
- To avoid merging the enterprise runtime directly onto a stale base, the
  integration branch `enterprise/wecom-runtime-v0.0.4` was created from
  `upstream/main`, then `enterprise/wecom-runtime` was merged into it.
  Resulting merge commit: `7eaa0fa`.

Conflict-resolution policy:

- Keep upstream `v0.0.4` lifecycle CLI, docs, and tests as source of truth.
- Do not reintroduce the old direct `agentseek gateway` contract in generated
  projects; the enterprise gateway startup script now calls `bub gateway`,
  while lifecycle entrypoints are exposed through `.agentseek/lifecycle.toml`.
- Add `.agentseek/lifecycle.toml` to both the enterprise template and the
  checked-in example, and update `.gitignore` so that file is preserved while
  other local `.agentseek` state remains ignored.
- Register `deepagents/enterprise-wecom` in the new template registry/docs.
- Keep this deployment note as internal handoff material. It is not intended
  for a clean upstream PR as-is; later PRs should split out generic pieces and
  remove company-specific deployment details.

Validation on the integration branch:

- `uv sync --locked --all-packages --all-extras --group plugins` passed.
- Unified regression suite passed:
  `PYTHONPATH="$PWD" uv run pytest --import-mode=importlib tests contrib/agentseek-enterprise/tests contrib/agentseek-wecom/tests contrib/agentseek-contextseek/tests contrib/agentseek-langchain/tests -q`
  -> `174 passed, 1 warning`.
- Template render tests passed:
  `uv run pytest tests/cli_commands/test_templates_render.py -q`
  -> `25 passed`.
- Focused lint passed across enterprise/wecom/example/probe files with
  `uv run ruff check --no-fix ...` -> `All checks passed!`.
- Template registry check passed:
  `uv run agentseek create deepagents --list-templates` shows
  `deepagents/enterprise-wecom`.
- Example lifecycle smoke passed from
  `examples/enterprise_wecom_digital_employee`:
  `uv run agentseek info` and
  `uv run agentseek dev --dry-run --skip-check`.

Mac mini update path for this branch:

```bash
git fetch origin
git switch enterprise/wecom-runtime-v0.0.4
git pull origin enterprise/wecom-runtime-v0.0.4
uv sync --locked --all-packages --all-extras --group plugins
```

Then run the local preflight/lifecycle checks before live WeCom traffic:

```bash
cd examples/enterprise_wecom_digital_employee
uv run agentseek info
uv run agentseek dev --dry-run --skip-check
uv run python scripts/prod_check.py --env-file .env
```

### v0.0.4 Mac mini verification — 3 blocking issues found and fixed (2026-06-29)

The lifecycle CLI checks pass (`agentseek info` / `dev --dry-run` / `prod_check`
all OK after the local workarounds below), but the first Mac mini pull found
that **`bub gateway` did not start**. Three integration issues surfaced by the
upstream lifecycle merge; all three have follow-up fixes in this branch:

1. **Leaked absolute path in example `pyproject.toml`** — `[tool.uv.sources]`
   has 6 hardcoded `/Users/sambazhu/工作/agentseek` (the Mac Pro repo path),
   which doesn't exist on the Mac mini → build fails. Worked around locally
   (sed → `/Users/sambazhu/agentseek-enterprise`). **Fixed:** the checked-in
   example now uses relative `[tool.uv.sources]` paths (`../..`,
   `../../contrib/...`), and its checked-in `uv.lock` uses the same relative
   editable sources instead of machine-specific absolute paths.

2. **`bub` crashes when Logfire is not configured** — `bub/__main__.py`
   `_instrument_bub()` wraps `logfire.configure()` in `except ImportError` only;
   with logfire installed but no token, `logfire.configure()` raises
   `LogfireConfigError`, which is NOT caught → the bub CLI exits before the
   gateway starts. Worked around locally with `LOGFIRE_TOKEN=foo` (configure
   passes, then 401 noise on span export). **Fixed locally:** the enterprise
   gateway now starts Bub through `scripts/bub_gateway.py`, which guards
   `logfire.configure()` before importing `bub.__main__` and falls back to
   `send_to_logfire=False` when Logfire is not configured.

3. **Env not reaching plugins under `bub gateway` (most critical)** —
   `run_gateway.sh` runs `bub gateway` from `REPO_ROOT`; `lifecycle.toml`'s
   `env_file = ".env"` resolves to the **repo-root `.env`** (which only contains
   the `AGENTSEEK_ENV_FILE=examples/.../.env` pointer), and the real config at
   `examples/.../.env` is NOT loaded. Result: schedule plugin disabled
   (`Need either "engine" or "url" defined` — `AGENTSEEK_SCHEDULE_SQLALCHEMY_URL`
   not read), wecom channel does not bind port 12000. Note: `agentseek info` run
   **from the example dir** reads the example `.env` fine, so the config is
   correct — the break is specifically the `bub gateway` REPO_ROOT-relative
   launch vs env-file resolution. **Fixed:** `run_gateway.sh` now passes
   `--env-file "$AGENTSEEK_ENV_FILE"` directly to `uv run`, so the actual
   example/project `.env` is loaded into the Bub process. The script still
   exports `AGENTSEEK_ENV_FILE` for code that needs to resolve the same file.

Follow-up validation on the Mac Pro:

- `env -u LOGFIRE_TOKEN uv run --offline --env-file examples/enterprise_wecom_digital_employee/.env --with jaydebeapi --with JPype1 python examples/enterprise_wecom_digital_employee/scripts/bub_gateway.py gateway --help`
  exits 0, proving the wrapper loads the Bub gateway command without Logfire
  credentials.
- `uv run --offline --env-file examples/enterprise_wecom_digital_employee/.env
  python -c ...` confirmed `AGENTSEEK_SCHEDULE_SQLALCHEMY_URL`,
  `AGENTSEEK_WECOM_CALLBACK_PATH`, and the LangChain spec are present in the
  child-process environment (only booleans were printed; no secrets).
- `PYTHONPATH="$PWD" uv run pytest tests/cli_commands/test_templates_render.py
  -q` -> `25 passed`.
- From `examples/enterprise_wecom_digital_employee`,
  `uv lock --check --offline` and
  `uv sync --locked --offline --no-install-project` both passed against the
  relative editable source paths.

### v0.0.4 re-verification on Mac mini (after 70b44d8) — fixes confirmed, 2 new regressions

After pulling 70b44d8, the 3 original blockers are fixed and `bub gateway`
boots cleanly:
- ① relative `[tool.uv.sources]` paths — build OK.
- ② `bub_gateway.py` wrapper — no `LogfireConfigError` crash.
- ③ `--env-file "$AGENTSEEK_ENV_FILE"` — env reaches plugins; **schedule channel
  loads** (`schedule.start complete`) and **wecom binds port 12000**.
- Identity works under the new lifecycle: sidecar starts (pid observed),
  `zhuchunlin` resolved + cached. No SIGBUS.

But the live `我是谁` turn exposed **2 new v0.0.4 regressions** (the upstream
lifecycle refactor changed config handling). Both now have follow-up fixes in
this branch:

1. **ContextSeek init fails → semantic memory disabled.**
   `error parsing value for field "recall_routes" from source "EnvSettingsSource"`.
   The `.env` value `AGENTSEEK_CTX_RETRIEVAL_RECALL_ROUTES=["vector"]` (a JSON
   array as a string) was not always passed to upstream ContextSeek in a shape
   pydantic-settings could parse. ContextSeek fell back to disabled.
   **Fixed:** the `agentseek-contextseek` alias layer now normalizes
   `AGENTSEEK_CTX_RETRIEVAL_RECALL_ROUTES` into a JSON list before setting
   `RETRIEVAL_RECALL_ROUTES`, including escaped/quoted JSON, comma-separated,
   and single-route forms.

2. **Model defaults to OpenRouter → turn fails (no reply).** The turn errors
   `[openrouter] No openrouter API key provided` even though `.env` has
   `AGENTSEEK_MODEL=glm-5.2` + `AGENTSEEK_MODEL_PROVIDER=openai` + DashScope
   key/base (and identity env IS loaded, so the `.env` reached the process).
   v0.0.4's model resolution wasn't applying the configured provider/model
   because an inherited `BUB_MODEL=openrouter:free` could outrank dotenv-only
   model config. **Fixed:** the enterprise `bub_gateway.py` wrapper now loads
   `AGENTSEEK_ENV_FILE` into `os.environ` before importing Bub, then mirrors
   project `AGENTSEEK_*` values into `BUB_*` values. This makes the project
   `.env` the authoritative runtime config source for Bub, LangChain, and
   ContextSeek. Verified with an inherited `BUB_MODEL=openrouter:free`:
   `BUB_MODEL` becomes `glm-5.2`, `BUB_LANGCHAIN_SPEC` is set, and
   `settings.build_model()` resolves `ChatOpenAI / glm-5.2 / DashScope base`.

Follow-up validation on the Mac Pro:

- `uv run pytest contrib/agentseek-contextseek/tests -q` -> `32 passed`.
- `uv run pytest --import-mode=importlib contrib/agentseek-enterprise/tests
  contrib/agentseek-wecom/tests contrib/agentseek-contextseek/tests
  contrib/agentseek-langchain/tests -q` -> `90 passed, 1 warning`.
- `PYTHONPATH="$PWD" uv run pytest tests/cli_commands/test_templates_render.py
  -q` -> `25 passed`.
- `env -u LOGFIRE_TOKEN uv run --offline --env-file examples/enterprise_wecom_digital_employee/.env --with jaydebeapi --with JPype1 python examples/enterprise_wecom_digital_employee/scripts/bub_gateway.py gateway --help`
  exits 0.
- A dummy-env probe confirmed inherited `BUB_MODEL=openrouter:free` is replaced
  by project `AGENTSEEK_MODEL=glm-5.2` / `BUB_MODEL=glm-5.2`, and
  `settings.build_model()` uses the DashScope-compatible OpenAI endpoint.
- A ContextSeek probe confirmed escaped
  `AGENTSEEK_CTX_RETRIEVAL_RECALL_ROUTES` resolves to `['vector']`.

### v0.0.4 re-verification (after 3cda1e9) — model+init fixed, seekdb scope still broken

Pulled 3cda1e9 and re-ran the full live sweep. The two regressions from the
previous round are fixed:
- **Model**: `我是谁 → 你好，朱春霖！` — glm-5.2/DashScope applies (no OpenRouter
  fallback). The `AGENTSEEK_* → BUB_*` sync in `bub_gateway.py` works.
- **ContextSeek init**: `seekdb has opened` + `ContextSeek client initialized`,
  no `recall_routes` parse error.

But the sweep exposed a **deeper ContextSeek regression** — seekdb semantic
memory is non-functional (no store, no retrieve):

- After `请长期记住：…数据架构`, the bot says "已长期记住" but **nothing is
  written to seekdb** (the `store/` dir has no new data, only old Jun-27 data).
- `我的工作职责是什么？` after a gateway restart (fresh session) returns a
  generic `你好，朱春霖！` — no recall. Within the same session it "recalls",
  but that's **session memory**, not seekdb.
- The log shows **no ContextSeek retrieve or save activity** (only the client
  init) — `build_prompt`/`save_state` appear to return early.

**Likely root cause:** ContextSeek's `_enterprise_employee_scope(state)` returns
`None` because the scoped keys (`tenant_key`/`user_key`) it reads from
`state["_langgraph_runtime_context"]["enterprise"]` aren't present. The v0.0.4
lifecycle refactor changed how the enterprise identity state is injected, so the
scoped keys don't reach ContextSeek → scope=None → `build_prompt` sets
`identity_required` and returns without retrieving; `save_state` returns without
storing. Identity itself resolves fine (zhuchunlin + employee_context), so the
break is specifically the **scoped-key handoff into the ContextSeek scope**.

Test status: identity ✅, model ✅, short-term memory ✅, **seekdb long-term ❌
(scope=None)**. MCP not yet tested.

**Follow-up fix:** `agentseek-contextseek` now treats
`state["employee_context"]` as a trusted fallback source for enterprise scope
derivation when `_langgraph_runtime_context` is missing. It calls the same
`agentseek_enterprise.runtime.enterprise_runtime_context(...)` helper used by
the enterprise plugin, so the resulting scope is still anonymous:
`enterprise/v1/<tenant-key>/<user-key>/semantic`, with no OA account or employee
name embedded. This fixes both ContextSeek hooks:

- `build_prompt` can retrieve semantic memory once identity state exists.
- `save_state` can store the final turn even if the LangGraph runtime-context
  handoff was not present in Bub state.

Validation on the Mac Pro:

- `uv run pytest contrib/agentseek-contextseek/tests -q` -> `35 passed`.
- `uv run pytest --import-mode=importlib contrib/agentseek-enterprise/tests
  contrib/agentseek-wecom/tests contrib/agentseek-contextseek/tests
  contrib/agentseek-langchain/tests -q` -> `93 passed, 1 warning`.
- `uv run ruff check --no-fix contrib/agentseek-contextseek` -> all checks
  passed.

### v0.0.4 seekdb deep-dive (after 448c6de) — scope+retrieve work, but build_prompt output doesn't reach the model

Pulled 448c6de (scope fallback). Added temporary DEBUG logging (`CTXDBG`
prefix, since reverted) to `build_prompt` to trace scope + retrieve. Findings:

- **Scope IS computed** (non-None): `source=runtime_context`,
  `enterprise/v1/hmac-9b9.../hmac-812.../semantic`. The scoped keys are present
  in `_langgraph_runtime_context["enterprise"]` — the fallback wasn't needed.
- **Retrieve works**: `query='我的工作职责是什么'` → `hits=5`. seekdb returned
  5 matches.
- **context_block is generated** from the 5 hits.
- BUT the model's reply is still a generic `朱春霖，你好！` — the retrieved
  context is NOT used.

Tested both injection modes:
- `INJECTION_MODE=state` (default): `build_prompt` puts context in
  `state["_contextseek_block"]` + returns None. The model doesn't see it.
- `INJECTION_MODE=prompt`: `build_prompt` returns the injected prompt text
  (context_block + query). The model STILL doesn't use it.

**Key contrast:** the enterprise plugin's `employee_context` DOES reach the
model (reply addresses 朱春霖). ContextSeek's `build_prompt` output does NOT.
They use different hooks/injection mechanisms — enterprise's works on v0.0.4,
ContextSeek's doesn't.

**Root cause:** v0.0.4's lifecycle refactor changed the prompt-building
pipeline. ContextSeek's `build_prompt` hook (retrieves + injects semantic
context) is not wired into the new prompt builder — its return value (prompt
mode) and its state field `_contextseek_block` (state mode) are not consumed
by the model's actual prompt. The retrieve + scope are fine; the break is the
**hook-output → model-prompt connection**.

**Fix direction for Codex:**
1. Check how v0.0.4's prompt builder consumes `build_prompt` hook returns +
   state fields.
2. Compare enterprise plugin (employee_context reaches model) vs ContextSeek
   (build_prompt doesn't) — find the hook/mechanism difference.
3. Wire ContextSeek's `_contextseek_block` (state mode) or `build_prompt`
   return (prompt mode) into v0.0.4's prompt builder.

### v0.0.4 ContextSeek prompt handoff fix (after 8b66a2f)

The final integration break was in the boundary between Bub prompt hooks and
the LangChain runnable spec. Bub's `build_prompt` hook is **first-result wins**:
it is useful for choosing a prompt, but it is not a composition point for
multiple context-producing plugins. In v0.0.4, relying on ContextSeek's
`build_prompt` return value meant the retrieved block could be computed yet
never reach the enterprise template's actual `RunnableSpec.build_input`.

Fix:

- `agentseek-langchain` now receives the Bub framework instance and, immediately
  before building `InvocationContext` / invoking the runnable spec, calls
  `build_prompt` hooks in `call_many(...)` mode as a **state enrichment pass**.
- ContextSeek's hook is idempotent per turn via `_contextseek_enriched`, so the
  old Bub prompt path and the new LangChain state-enrichment path cannot cause
  duplicate seekdb retrievals.
- The enterprise WeCom template already consumes `state["_contextseek_block"]`
  as a `SystemMessage` beside `employee_context` and short-term memory. That is
  now the canonical path: `seekdb retrieve -> _contextseek_block -> SystemMessage
  -> model`.
- The old prompt-return path is still left intact for non-enterprise/simple
  runtimes, but the enterprise runtime no longer depends on it.

Validation on the Mac Pro:

- `uv run pytest --import-mode=importlib
  contrib/agentseek-langchain/tests/test_plugin.py
  contrib/agentseek-contextseek/tests/test_plugin.py -q` -> `27 passed`.
- `uv run pytest --import-mode=importlib contrib/agentseek-enterprise/tests
  contrib/agentseek-wecom/tests contrib/agentseek-contextseek/tests
  contrib/agentseek-langchain/tests -q` -> `95 passed, 1 warning`.
- `uv run ruff check --no-fix contrib/agentseek-langchain
  contrib/agentseek-contextseek` -> all checks passed.

Mac mini live re-verification should focus on:

1. ask a query that should hit seekdb, e.g. `我的工作职责是什么`;
2. confirm logs still show `retrieve hits > 0`;
3. confirm the answer uses retrieved semantic memory, not only
   `employee_context`;
4. confirm a repeated turn does not duplicate seekdb retrieve within the same
   agent turn.

### v0.0.4 FULL VERIFICATION PASSED (2026-06-29, after 6391994)

All integration issues resolved. Full live sweep on the Mac mini, all PASS:

1. **Identity + model**: `我是谁 → 朱春霖` (sidecar DM + glm-5.2/DashScope). ✅
2. **Short-term memory**: `帮我记一下…出差` → recalled. ✅
3. **seekdb long-term store**: `请长期记住：…数据架构` → written to seekdb
   (new sstable data). ✅
4. **seekdb retrieve + enrichment**: `我的工作职责是什么？` → reply includes
   "职责：**负责数据架构工作**" (the stored semantic memory, beyond the DM
   employee_context). ✅
5. **MCP tools**: `列一下当前可用的 MCP 工具` → listed gildata (9 aggregate
   tools + 300+ API endpoints). ✅

Zero SIGBUS throughout. The v0.0.4 branch is production-ready.

**Note on the enrichment investigation:** the "build_prompt output not reaching
model" diagnosis (which prompted commit 6391994) was likely a false alarm — the
seekdb enrichment was probably working since the scope fix (448c6de). The
appearance of failure was caused by **`grep` truncating multi-line loguru
`content=` output** (the reply's first line is a greeting; the seekdb-enriched
content follows on subsequent lines that `grep | tail -1` misses). Always use
`grep -A N` when reading multi-line WeCom replies from the gateway log.
6391994 is harmless (adds `_contextseek_enriched` dedup + explicit enrichment
call) — Codex can decide whether to keep or simplify.

### Production freeze baseline review (2026-06-29, tag enterprise-wecom-v0.0.4-prod-20260629)

Checked out tag `enterprise-wecom-v0.0.4-prod-20260629` (commit `6cd8d41`,
doc-only successor of verified `0c63850`). Full production baseline review on
the Mac mini:

**Preflight**: `prod_check.py --env-file .env` → passed (0 failures, 0
warnings). `.env` confirmed: sidecar + seekdb + identity cache + LangSmith off.

**Live smoke test (8 messages, all PASS)**:

| Test | Messages | Result |
|------|----------|--------|
| A. Identity | `我是谁` | ✅ 朱春霖 + OA + 组织路径 + 岗位 |
| B. Short-term (P1) | `帮我记一下…深圳出差` → `我刚才说我要去哪里？` | ✅ "明天去深圳出差" |
| C. Explicit long-term (SQLiteStore) | `请长期记住：我偏好简洁、分点的回复方式` → `你记得我的回复偏好吗？` | ✅ "简洁/最多三条要点/分点" |
| D. Semantic long-term (seekdb) | `请长期记住：我的职责是负责数据架构工作` → `我的工作职责是什么？` | ✅ "职责：负责数据架构工作" |
| E. MCP tools | `列一下当前可用的 MCP 工具` | ✅ 4 services (gildata ×3 + tavily) |

**Health**: alive ✓, sidecar PID stable ✓, 0 SIGBUS/exit 138, msgid dedup
fired 2× (normal). The production freeze baseline is confirmed production-ready.

### Template rendered standalone project — VERIFIED (2026-06-29, v0.0.4)

Rendered a clean standalone project via `agentseek create deepagents/enterprise-wecom`
from the v0.0.4 template (commit `6ec9ff3`), copied in `.env` + DM jar +
`mcp.local.json`, `uv sync`, ran `scripts/run_gateway.sh` (bub gateway via
`bub_gateway.py` wrapper). Full A-E smoke test, all PASS:

| Test | Result |
|------|--------|
| A. Identity | ✅ 朱春霖 + OA + 组织路径 + 岗位 + 角色 |
| B. Short-term (P1) | ✅ "去深圳出差" recalled |
| C. Explicit long-term (SQLiteStore) | ✅ "简洁、分点的回复方式" recalled |
| D. Semantic long-term (seekdb) | ✅ "核心职责：负责数据架构工作" |
| E. MCP tools | ✅ gildata (9 aggregate + 300+ API) |

**Health**: alive ✓, sidecar PID 24320 stable ✓, 0 SIGBUS/exit 138, msgid
dedup 28× (normal). Preflight passed (0 failures, 0 warnings).

Both the Example (in-repo) and the rendered standalone project are fully
verified on v0.0.4. The template produces a deployable standalone project
under the new `bub gateway` lifecycle.

### GA release + company GitLab mirror (2026-06-30)

Published the final GA release:

- GitHub release:
  `https://github.com/sambazhu/agentseek-enterprise/releases/tag/enterprise-wecom-v0.0.4-ga-20260629`
- GA tag: `enterprise-wecom-v0.0.4-ga-20260629` -> `1b06692`
- Production branch: `enterprise/wecom-runtime-v0.0.4` -> `1b06692`
- Previous audit tag: `enterprise-wecom-v0.0.4-prod-20260629` -> `6cd8d41`

Mirrored the production refs to the company GitLab project:

- GitLab remote:
  `http://172.200.6.12:9091/zhuchunlin/agentseek-enterprise.git`
- Pushed branch: `enterprise/wecom-runtime-v0.0.4`
- Pushed tags:
  `enterprise-wecom-v0.0.4-ga-20260629`,
  `enterprise-wecom-v0.0.4-prod-20260629`

Remote verification:

```text
refs/heads/enterprise/wecom-runtime-v0.0.4 -> 1b066927...
refs/tags/enterprise-wecom-v0.0.4-ga-20260629^{} -> 1b066927...
refs/tags/enterprise-wecom-v0.0.4-prod-20260629^{} -> 6cd8d41f...
```

Internal deployments can now clone from GitLab and pin the GA tag:

```bash
git clone http://172.200.6.12:9091/zhuchunlin/agentseek-enterprise.git
cd agentseek-enterprise
git checkout enterprise-wecom-v0.0.4-ga-20260629
```

### Memory SQLAlchemy store migration — VERIFIED (2026-06-30, branch enterprise/memory-sqlalchemy-store)

Branch `enterprise/memory-sqlalchemy-store` (based on v0.0.4 GA `88ebf79`,
adds commit `d1b3614`). Verified the migration of short-term + explicit
long-term memory from SQLite fallback to SQLAlchemy URL on the Mac mini.

**Part A — code-level tests**: enterprise 36 passed, template render 25 passed,
docs-test mkdocs build OK.

**Part B — SQLite fallback (no SA URLs set)**: A-E smoke test all PASS —
identity, short-term, explicit long-term (SQLiteStore), seekdb semantic,
MCP. No regression from the v0.0.4 GA baseline.

**Part C — SQLAlchemy path (SA SQLite URLs)**:
```
AGENTSEEK_ENTERPRISE_MEMORY_SQLALCHEMY_URL=sqlite+pysqlite:///./runtime/enterprise-short-term-memory-sa.sqlite3
AGENTSEEK_ENTERPRISE_STORE_SQLALCHEMY_URL=sqlite+pysqlite:///./runtime/enterprise-long-term-store-sa.sqlite3
```
7-step smoke test all PASS:

| Step | Result |
|------|--------|
| 1. Identity | ✅ 朱春霖 |
| 2-3. Short-term store+recall | ✅ |
| 4-5. Explicit long-term store+recall (SA) | ✅ |
| 6. **Restart → persistence** | ✅ SA store recalled across restart |
| 7. MCP tools | ✅ 4 services |

SA SQLite files confirmed created (`runtime/*-sa.sqlite3`). 0 SIGBUS, 0
SQLAlchemy init errors, 0 WeCom duplicate turns. seekdb semantic memory
unaffected (ContextSeek uses its own SeekDB, not the enterprise SA store).

### v0.0.5 RC1 verification (2026-06-30, enterprise-wecom-v0.0.5-rc1)

RC tag `enterprise-wecom-v0.0.5-rc1` at commit `3d0644c`. Verified on Mac mini
with PostgreSQL 17 (`postgresql+psycopg://localhost/agentseek`).

**Part A — code-level tests**: enterprise 36 passed, template render 25 passed,
docs-test mkdocs build OK.

**Live smoke test (7 messages, all PASS)**:

| # | Message | Reply | Result |
|---|---------|-------|--------|
| 1 | `我是谁` | 朱春霖 + OA + 岗位 + 组织路径 | ✅ |
| 2 | `帮我记一下…深圳出差` | "已长期记住：明天去深圳出差" | ✅ (correctly stored 深圳) |
| 3 | `我刚才说我要去哪里？` | "明天去深圳出差 ✈️" | ✅ short-term recall |
| 4 | `请长期记住：偏好简洁分点` | "已长期记住：偏好简洁、分点的回复方式" | ✅ long-term store |
| 5 | `你记得我的回复偏好吗？` | "记得你的长期偏好：企微回复简洁 / 分点呈现" | ✅ **NO 出差 mixed in** |
| 6 | `我的工作职责是什么？` | "岗位：团队长兼数据架构师 / 职责：数据架构设计…" | ✅ seekdb semantic |
| 7 | `列一下当前可用的 MCP 工具` | 4 services (gildata×3 + tavily) | ✅ |

**Health**: alive ✓, sidecar PID 34525 stable ✓, 0 SIGBUS/exit 138, 0
SQLAlchemy init errors, 2 msgid dedup (normal WeCom retries).

All acceptance criteria met. **enterprise-wecom-v0.0.5-rc1 is ready for GA tagging.**

### MCP policy multi-risk verification (2026-06-30, enterprise/wecom-mcp-policy-audit)

Configured 3 risk levels across 5 MCP servers:
- `AGENTSEEK_ENTERPRISE_MCP_WRITE_TOOLS=tavily-search/*` (write)
- `AGENTSEEK_ENTERPRISE_MCP_RISKY_TOOLS=gildata_datamap-data/*,agent-platform/install_agent_skills` (risky)
- All other tools: read (default allow)

**Round 1 — read + risky triggered together**:
| # | Audit event | Risk | Tool |
|---|-------------|------|------|
| 1 | succeeded | read | gildata_datamap-api/CompanyBasicInfo |
| 2 | succeeded | read | gildata_datamap-tools/FinQuery |
| 3 | confirmation_required | risky | gildata_datamap-data/FinGeneralQuery |
| 4 | succeeded | read | gildata_datamap-api/CompanyBasicInfo |

Read tools executed directly (no confirmation). Risky tool (FinGeneralQuery)
blocked — model proactively asked "是否确认执行？" without calling it.

**Round 2 — write tool (tavily_search)**:
Model tried 3 gildata read tools (no international news data), then wanted
tavily_search (write) → asked "是否确认执行？" (no execution). Audit entries
5-7: read tools succeeded.

**Round 3 — confirmed execution**:
User sent "确认" → tavily_search called with `confirmed=True` → executed (search
results returned). Audit entry 9: `succeeded, risk=write, confirmed=True`.

**Full audit log** (9 events):
```
 1. succeeded              risk=read    confirmed=False   gildata/CompanyBasicInfo
 2. succeeded              risk=read    confirmed=False   gildata/FinQuery
 3. confirmation_required  risk=risky   confirmed=False   gildata/FinGeneralQuery
 4. succeeded              risk=read    confirmed=False   gildata/CompanyBasicInfo
 5. succeeded              risk=read    confirmed=False   gildata/NewsDataQuery
 6. succeeded              risk=read    confirmed=False   gildata/MacroNewslist
 7. succeeded              risk=read    confirmed=False   gildata/NewsInfoList
 8. (tavily write — model proactively asked confirmation, no tool call)
 9. succeeded              risk=write   confirmed=True    tavily/tavily_search
```

**Sensitive field redaction**: no token/password/secret/api.key found in audit
entries ✅.

**Known issue**: Round 3's reply was generated in the gateway log (international
news results) but WeCom showed "抱歉，我暂时无法回答" — the model took ~19 min
processing tavily search results, causing a **WeCom stream timeout**. This is a
WeCom delivery issue (stream response expiry), NOT an MCP policy issue. The
policy enforcement + audit logging are correct.

**Health**: 0 SIGBUS, 0 SQLAlchemy errors, 2 msgid dedup (normal), gateway
stable.

## The DM connection root cause + fix (the big one)

**Symptom:** the DM JDBC bridge (`jaydebeapi` + JPype + `DmJdbcDriver`) could
NOT connect to the DM (`192.10.50.26:5236`) — `网络通信异常` /
`SocketTimeoutException: Read timed out`. DBeaver (same driver jar, same URL,
same creds, same Java 21, same direct path) connected fine and queried data.

**Root cause (confirmed by tcpdump):** FlClash in TUN mode intercepts Java's
raw TCP to the DM. With FlClash running, `tcpdump` showed **0 packets** for
the Java SYN (it never hit the wire); with FlClash quit, the same Java
connected in ~180 ms with a full clean handshake. DBeaver worked because it
uses the macOS *system proxy* (which FlClash forwards correctly); the JDBC
bridge uses raw sockets that don't use the system proxy but get swallowed by
FlClash's TUN capture.

This was mis-diagnosed for a long time. **Ruled out (do not re-investigate):**
driver version (8.1.2.192 and 8.1.3.62 both behave identically), Java version,
schema/database/SSL/compatibleMode URL params, `--add-opens`, IPv4/IPv6 stack,
SOCKS proxy, concurrent-session limit, MAC randomization, the Bash sandbox.
The cause was FlClash, full stop.

**Fix — keep FlClash TUN OFF (system-proxy mode).** This is the practical
resolution on this machine. TUN mode intercepts *all* of the gateway's traffic,
and two destinations cannot go through FlClash's proxy egress:

1. **The DM (`192.10.50.26:5236`)** — FlClash swallows Java's raw TCP to it
   (tcpdump: 0 packets). Fixed by TUN-off, or — if TUN must be on — a `/32`
   static host route that overrides FlClash's split routes by longest-prefix
   match: `sudo route add -host 192.10.50.26 172.20.199.254` (persisted via the
   launchd daemon below).
2. **The WeCom self-built-app API (`qyapi.weixin.qq.com`, `openuserid_to_userid`)**
   — the app has an IP allowlist. Through FlClash's proxy the egress is the
   proxy node's IP (e.g. `45.207.34.86`), which is NOT allowlisted →
   `errcode=60020 "not allow to access from your ip"` → `open_userid` can't be
   resolved → identity fails. This is a *domain* (FlClash fake-ip), so a `/32`
   route does NOT bypass it — **only TUN-off does** (direct egress = the Mac
   mini's real public IP, which is allowlisted; verified returning `zhuchunlin`).

With FlClash **TUN off (system-proxy mode)**, the gateway's traffic (DM raw TCP,
WeCom API via urllib, the model) all go **direct** — they don't use the macOS
system proxy — so DM + WeCom API + model all work; FlClash still proxies
system-proxy-aware apps (browser, etc.). Verified end-to-end:
`我是谁 → 你好，朱春霖！` (full employee context) with FlClash running in
system-proxy mode. **Recommendation: on this machine, never enable FlClash TUN.**

If TUN absolutely must stay on (some app needs it): the DM is handled by the
`/32` route below, but the WeCom API additionally needs an OS-level bypass —
pin `qyapi.weixin.qq.com` to a real IP in `/etc/hosts` and add a `/32` route
for it (same mechanism as the DM, to defeat FlClash's fake-ip). A FlClash
`DIRECT` *rule* does not work (tested — it failed to merge into the active
config, and even merged, TUN still captures the packet first).

`/32` DM route + launchd persistence (only needed if TUN is on):
```bash
sudo route add -host 192.10.50.26 172.20.199.254
sudo cp examples/enterprise_wecom_digital_employee/launchd/com.local.dm-direct-route.plist /Library/LaunchDaemons/
sudo chown root:wheel /Library/LaunchDaemons/com.local.dm-direct-route.plist
sudo launchctl load -w /Library/LaunchDaemons/com.local.dm-direct-route.plist
```

> Note for other machines: the FlClash interception is Mac-mini-specific (the
> dev Mac Pro, wired/without FlClash, never had this). On any machine running
> FlClash/clash TUN, prefer TUN-off for the gateway; if TUN is required, add
> `192.10.50.0/24` to TUN `route-exclude-address` AND pin the WeCom API domain
> direct (a `DIRECT` rule is NOT enough).

## Working configuration (this example's `.env`)

- `AGENTSEEK_IDENTITY_DM_DRIVER_MODULE=agentseek_enterprise.identity.jdbc_driver`
- `AGENTSEEK_IDENTITY_DM_JDBC_JAR=vendor/dameng/DmJdbcDriver18-8.1.3.62.jar`
  (8.1.2.192 also works once FlClash isn't intercepting)
- `AGENTSEEK_IDENTITY_DM_JDBC_JAVA_HOME=/opt/homebrew/opt/openjdk@11/libexec/openjdk.jdk/Contents/Home`
  — **must be Java 11.** JPype 1.7.1 + Java 21 throws
  `ExceptionInInitializerError` on `java.sql.Types`.
- `AGENTSEEK_IDENTITY_DM_EXECUTION_MODE=subprocess` — run the JDBC lookup in a
  child process so the gateway process does not load `libjvm`.
- `AGENTSEEK_IDENTITY_DM_SUBPROCESS_TIMEOUT_SECONDS=30`.
- `AGENTSEEK_ENTERPRISE_IDENTITY_CACHE_ENABLED=true` — cache successful
  employee identity lookups in the gateway process.
- `AGENTSEEK_ENTERPRISE_IDENTITY_CACHE_TTL_SECONDS=600`.
- `AGENTSEEK_ENTERPRISE_IDENTITY_CACHE_MAX_ENTRIES=1024`.
- `AGENTSEEK_CTX_STORAGE_BACKEND=seekdb` — target configuration after
  subprocess isolation. Use `memory` only as a temporary rollback if the Mac
  mini still shows a JVM/ONNX crash.
- `AGENTSEEK_IDENTITY_DM_HOST=192.10.50.26`, port `5236`, user `dbo`.
- Root `.env` contains ONLY `AGENTSEEK_ENV_FILE=examples/enterprise_wecom_digital_employee/.env`.

`dmPython` is unavailable on macOS arm64 (no wheel, no native DM client lib),
so the JDBC bridge (jaydebeapi + JPype1 + the DmJdbcDriver jar + a JDK) is the
only way to reach the DM from a Mac.

## Launch (from repo root, FlClash may be on if the route is active)

```bash
mkdir -p runtime   # one-time; the ./runtime/*.db paths need it
examples/enterprise_wecom_digital_employee/scripts/run_gateway.sh
```

`--offline` is a safe fallback (deps are cached). With FlClash in system-proxy
mode (the recommended TUN-off setup), uv can also fetch normally through the
live proxy at `127.0.0.1:7890`; `--offline` is only required when FlClash is
fully quit (the dead system proxy then breaks uv's fetch).

Probe one identity without the gateway:
```bash
uv run --env-file examples/enterprise_wecom_digital_employee/.env \
  --with jaydebeapi --with JPype1 \
  python scripts/probe_staff_identity.py --oa <account> --source python-db
```

## Known issues / workarounds

1. **JVM + ONNX SIGBUS crash.** `jaydebeapi`→JPype→`libjvm` (DM JDBC) AND
   ContextSeek's `onnxruntime` (seekdb embedding) in the **same Python
   process** crash with SIGBUS (exit 138) on every message that touches both.
   Confirmed via macOS DiagnosticReports (both `libjvm.dylib` and
   `onnxruntime_pybind11_state.so` loaded). The fix is implemented as
   subprocess identity mode: the child process loads JPype/JDBC; the gateway
   process can load SeekDB/ONNX. Roll back to `STORAGE_BACKEND=memory` only if
   live testing still shows a crash.
2. **JPype + Java 21 incompatible** → use Java 11 (above).
3. **WeCom retry churn.** Fixed in `agentseek-wecom`: text/voice retries with
   the same WeCom `msgid` reuse the original stream response instead of
   launching duplicate agent turns.
4. **`uv` + FlClash system-proxy residue.** Killing FlClash leaves the macOS
   system HTTP/SOCKS proxy pointing at a dead `127.0.0.1:7890`; uv (Rust/reqwest)
   honors it and fails to fetch. `curl` does NOT honor it (so direct endpoints
   still work). Keep FlClash up, or use `--offline`, or clear the system proxy.

## Verification: WeCom retry dedup (2026-06-27, Mac mini)

**Goal:** confirm the retry-churn fix works live, and characterize when
`wecom.duplicate_msgid` does/doesn't fire.

**Method:** added a diagnostic log in `_handle_plain_message`
(`wecom.incoming msgtype={} msgid={}`) to see the msgtype of every decrypted
POST. Restarted the gateway, sent a slow-reply prompt (long-output request →
generation >5 s, triggering WeCom retries).

**Result — msgtype distribution for one slow-reply message:**
- `1 × msgtype=text` (the original message; only one)
- `14 × msgtype=stream` (WeCom polling the stream for the reply; each poll
  carries its own msgid)
- agent turns = 1, replies = 1, `wecom.duplicate_msgid` = 0

**Conclusion:**
- The flood is gone: 15 POSTs → 1 agent turn → 1 reply (vs ~10 replies before
  the fix). WeCom now **polls** (`msgtype=stream` → `_handle_stream_poll`,
  returns the in-progress stream content without a new turn) instead of
  re-sending the message, because the gateway now returns a proper stream
  response (`_stream_response`). This is the primary mechanism that fixed the
  churn.
- `wecom.duplicate_msgid` (the msgid→stream dedup in
  `_get_or_create_stream_for_message`) fires only on a duplicate `msgtype=text`
  POST with the same msgid **while the stream is alive** (TTL
  `cache_ttl_seconds`, default 3600 s). Live, WeCom sends no duplicate text (it
  polls), so this path isn't exercised in normal operation — it's a safety net
  for the text-re-send edge case, covered by the two unit tests in
  `contrib/agentseek-wecom/tests/test_channel.py` (in-flight duplicate reuses
  the stream; after TTL expiry, reprocessing is allowed).
- The `wecom.incoming msgtype=...` diagnostic is intentionally left in place at
  debug level for future WeCom-behavior investigations.

## Completed production milestones

- **Long-lived DM sidecar / connection pooling:** verified live with
  `AGENTSEEK_IDENTITY_DM_EXECUTION_MODE=sidecar`. The gateway keeps `libjvm` out
  of the main process, cache misses reuse one long-lived JSON-lines worker, and
  the Mac mini run showed no SIGBUS / exit 138 / stale connection errors.
- **Production hardening:** `scripts/prod_check.py --env-file .env` passed with
  no warnings in the rendered project. The generated LaunchAgent was installed,
  starts at load, and restarts the gateway after a kill in about two seconds.
- **Clean `agentseek create` project:** `agentseek create
  deepagents/enterprise-wecom` rendered a standalone project that passed live
  WeCom identity, short-term memory, seekdb long-term memory, restart
  persistence, MCP listing, retry dedup, and JVM-isolation checks.
- **Upstream v0.0.4 integration:** `enterprise/wecom-runtime-v0.0.4` was
  created from `upstream/main` (`v0.0.4`) and merged with the production-ready
  enterprise branch. Lifecycle CLI/template compatibility was validated before
  asking the Mac mini deployment to move to the new branch.

## Recommended next work

- **Long-running MCP result delivery:** MCP policy and audit are implemented and
  verified. The next gap is WeCom delivery for slow tools: confirmed
  `tavily_search` can execute successfully, but very large search results made
  the model spend about 19 minutes before the final reply, exceeding the WeCom
  stream response window. Add a result-budget strategy such as tool-output
  truncation, answer-first summarization, or an async "正在处理" workflow before
  broad rollout of slow external tools.
- **Observability:** tracing is intentionally disabled in production for now
  (`LANGSMITH_TRACING=false`). Add Langfuse or another approved trace backend
  after the deployment endpoint and credentials are available.
- **Backup and rotation runbook:** document backup/restore for SQLite state,
  seekdb data, launchd logs, and namespace-secret rotation. Rotating
  `AGENTSEEK_ENTERPRISE_NAMESPACE_SECRET` changes the derived long-term-memory
  namespace, so do it before broad rollout or plan a migration.

## Files added/changed in this deployment session (for the Mac Pro pull)

- `examples/enterprise_wecom_digital_employee/DEPLOYMENT_NOTES.md` — this file.
- `examples/enterprise_wecom_digital_employee/launchd/com.local.dm-direct-route.plist`
  — the route-persistence daemon template.
- `examples/enterprise_wecom_digital_employee/launchd/com.local.agentseek-enterprise-wecom.plist`
  — user LaunchAgent template for keeping the gateway process alive.
- `examples/enterprise_wecom_digital_employee/scripts/run_gateway.sh`
  — shared gateway startup script for manual runs and launchd.
- `examples/enterprise_wecom_digital_employee/scripts/prod_check.py`
  — redacted production preflight and namespace-secret generator.
- `vendor/dameng/DmJdbcDriver18-8.1.3.62.jar` — newer DM JDBC driver (optional;
  8.1.2.192 also works once FlClash isn't intercepting).
- `agentseek-enterprise` now supports
  `AGENTSEEK_IDENTITY_DM_EXECUTION_MODE=subprocess` via
  `agentseek_enterprise.identity.dm_staff_sidecar`, so the gateway process can
  keep JPype/libjvm out of the main ContextSeek/ONNX process.
- `agentseek-enterprise` now supports
  `AGENTSEEK_ENTERPRISE_IDENTITY_CACHE_*` for short-TTL successful
  `EmployeeContext` caching in the gateway process.
- `agentseek-enterprise` now supports
  `AGENTSEEK_IDENTITY_DM_EXECUTION_MODE=sidecar` for a long-lived local
  JSON-lines worker that keeps the DM/JDBC connection warm while preserving
  JVM isolation from the gateway process.
- `agentseek-enterprise` runtime logs are routed through a loguru-aware adapter,
  so gateway logs can show sidecar start/stop and identity-cache diagnostics.
- `agentseek-wecom` keeps `wecom.incoming msgtype=...` as a debug-level
  diagnostic, reducing normal gateway log noise.
- The `.env` files are gitignored (secrets); copy the working `.env` to the Mac
  Pro manually if you want the same config.

### Memory prompt fix + PostgreSQL verification (2026-06-30, 0cfeaf7)

Pulled `0cfeaf7 Tighten enterprise memory layer prompts` (short-term memory
boundary, "三层记忆不要混答" system prompt, `[DurableEmployeeMemory]` label).
Verified on Mac mini with **PostgreSQL 17** (`postgresql+psycopg://localhost/agentseek`):

Targeted 4-message test:

| Step | Reply | Result |
|------|-------|--------|
| 1. `帮我记一下…出差` | "已长期记住：出差" | ✅ store |
| 2. `我刚才说我要去哪里？` | "出差" | ✅ short-term recall |
| 3. `请长期记住：偏好简洁分点` | "已长期记住：偏好" | ✅ long-term store |
| 4. `你记得我的回复偏好吗？` | "记得你的长期偏好：简洁/分点" | ✅ **NO 出差 mixed in** |

Before the fix, step 4's reply included "明天去出差" (short-term memory
leaked into the long-term preference answer). After the fix, step 4 ONLY
answers the preference — the three-layer memory boundary works.

PostgreSQL path confirmed: 0 SIGBUS, 0 SQLAlchemy init errors, 0 duplicate
turns. psycopg driver, agentseek database, brew services all OK.

### v0.0.5 GA tag (2026-06-30)

Published the v0.0.5 GA baseline after the RC1 smoke test passed on Mac mini:

- GA tag: `enterprise-wecom-v0.0.5-ga-20260630` -> `5cce3a2`
- RC tag: `enterprise-wecom-v0.0.5-rc1` -> `3d0644c`
- Production branch: `enterprise/wecom-runtime-v0.0.4` -> `5cce3a2`

This GA baseline includes the SQLAlchemy memory-store path, PostgreSQL
verification, and the tightened memory-boundary prompt that prevents short-term
travel context from leaking into explicit long-term preference answers.

### MCP policy and audit implementation (2026-06-30)

Started branch `enterprise/wecom-mcp-policy-audit` from the v0.0.5 GA baseline.
The branch adds a local MCP policy layer before the generated
`call_mcp_tool` adapter calls remote MCP servers:

- allowlist and denylist patterns using `server/tool`, `server:tool`, or
  wildcards;
- read, write, and risky tool classification;
- confirmation-required flow for write/risky tools before execution;
- JSONL audit records with redacted arguments;
- example, template, and reference documentation for the new runtime settings.

Local tests cover policy evaluation, policy-file/env merging, confirmation
messages, and audit redaction. Live WeCom validation is still required before
this branch can become the next GA baseline.

### MCP policy + audit verification (2026-06-30, enterprise/wecom-mcp-policy-audit)

Branch `enterprise/wecom-mcp-policy-audit` (commit `f2130a0`). Verified MCP
policy enforcement + audit logging on Mac mini with PostgreSQL + 5 MCP servers.

**A. No regression**: A-E smoke test all PASS (identity, short-term, explicit
long-term with no 出差 mixed in, seekdb semantic, MCP list).

**B. Risk/policy labels**: MCP tools evaluated with `risk=read` (gildata,
default allow) or `risk=write` (tavily, configured via
`AGENTSEEK_ENTERPRISE_MCP_WRITE_TOOLS=tavily-search/*`).

**C. Write tool confirmation**: `tavily_search` first call →
`confirmation_required` (tool NOT executed, model asked user "是否确认执行？").
Gildata read tools executed normally (`succeeded, risk=read`).

**D. Confirmed execution**: After user confirmed, `tavily_search` called with
`confirmed=True` → `succeeded, risk=write, confirmed=True` (tool executed).

**E. Audit log** (`runtime/mcp-audit.jsonl`): all event types present —
`succeeded` (read + write), `confirmation_required`. Sensitive fields
(token/password/secret/api.key) redacted — none found in audit entries.

**F. Health**: 0 SIGBUS, 0 SQLAlchemy errors, 2 msgid dedup (normal), gateway
stable throughout.

### WeCom stream timeout on confirmed MCP tool calls (2026-07-01, known issue)

**Symptom**: After user confirms a risky/write tool call (sends "确认"), the
gateway executes the tool + generates a reply, but the WeCom client shows
"抱歉，我暂时无法回答你的问题，请稍后再试" (fallback). The reply is in the
gateway log but never delivered to WeCom.

**Detailed timeline** (Round 2 confirmed tavily_search, 2026-07-01):

```
09:41:50  tavily_search executed (confirmed=True, risk=risky)
          audit: succeeded, returned 10 results (BBC, CNN, etc.)
09:42:29  model generated reply: "朱春霖，今日（7/1）国际新闻要点如下：..."
          (only 39 seconds after tool call — model is NOT slow)
09:47:13  WeCom started stream polls (321 polls, 1/sec) — 5 min AFTER reply
09:47:22  polls stopped (stream expired)
```

**Root cause**: The WeCom stream response has a timeout (~15-30s from the
original "确认" message). The full chain — model calls tavily_search (network
API call) → tavily returns 10 results (`max_results=10, search_depth=advanced`)
→ model processes results → model generates reply — exceeds the stream timeout
window. By the time the reply is written to the stream (09:42:29), the stream
has already expired. WeCom's later retries (09:47:13, 321 polls) find no new
reply for the expired stream.

**Key finding**: The tool call + policy + model are all correct:
- `confirmation_required` → `succeeded (confirmed=True)` ✅
- Tool returned real search results ✅
- Model generated reply in 39 seconds ✅
- Only the WeCom stream delivery failed ❌

**Impact**: Any confirmed tool call that involves a network API call (tavily,
agent-platform) + result processing will likely hit this timeout, because the
total chain (confirm → tool → process → reply) exceeds WeCom's stream window.

**Possible fixes for Codex**:
1. **Pre-write a "processing" placeholder** to the stream before the tool call,
   so WeCom sees activity and doesn't timeout.
2. **Stream the model's output** (chunked) instead of writing the full reply
   at the end.
3. **Reduce tavily search params** (`max_results=3, search_depth=basic`) to
   shorten processing — but this is a workaround, not a fix.
4. **Async delivery**: execute the confirmed tool asynchronously, deliver the
   result as a new WeCom message (not via the original stream).
5. **Increase stream timeout** if the WeCom channel has a configurable timeout.

### WeCom stream timeout fix (2026-07-01, pending Mac mini verification)

Implemented the low-risk channel-side fix in `agentseek-wecom`: inbound WeCom
callbacks now create the stream, schedule the Bub/agent processing in a
background task, and return the initial stream envelope quickly. The first
response contains the existing "已收到，正在处理..." placeholder with
`finish=false`; later WeCom `msgtype=stream` polls read the final model reply
from the same stream when the background turn completes.

This avoids holding the original "确认" HTTP callback open while the confirmed
MCP tool runs. It directly targets the observed failure mode where the tool and
model succeeded, but WeCom did not receive a usable stream id until the callback
had already exceeded its short response window.

Local verification:
- `make test-wecom` ✅
- `make typecheck-wecom` ✅
- `ruff` on WeCom channel/tests ✅

Mac mini live verification still required: repeat the confirmed `tavily_search`
flow and confirm the user sees the placeholder quickly, then receives the final
international-news reply through stream polling instead of the WeCom fallback.

### WeCom stream placeholder fix — VERIFIED (2026-07-01, commit 0488ede)

Codex's fix (`0488ede Return WeCom stream placeholder before slow turns finish`):
WeCom channel now creates a stream + schedules the agent turn in the
background, returning a placeholder ("已收到，正在处理...") immediately so
WeCom's stream polling stays alive during long tool calls.

**Full end-to-end test** (confirmed tavily weather search):

| Step | Message | Result |
|------|---------|--------|
| 1 | `搜索一下今天天气预报` | Reply delivered ✅ (model asked which city) |
| 2 | `深圳` | Reply delivered ✅ (model asked to confirm tavily_search) |
| 3 | `确认` | tavily_search executed (confirmed=True, succeeded) ✅ |
| 3b | (stream polling) | Weather reply delivered to WeCom ✅ (no timeout!) |

Audit entry #5: `succeeded, risk=risky, confirmed=True, tavily-search/tavily_search`

**Comparison with pre-fix** (same tavily confirmed call):
- Before: reply generated in 39s, but WeCom stream expired → "抱歉" fallback
- After: reply generated + delivered via stream polling → user sees weather results

**Note**: A separate DashScope `data_inspection_failed` (400) error occurred
when tavily returned AI tech news results — the model API's content moderation
blocked the output. This is a model API issue, NOT a gateway/policy/stream issue.
Weather search results did not trigger content moderation and delivered successfully.

**Health**: 0 SIGBUS, 0 stream timeouts, gateway stable.

### v0.0.6 RC1 smoke test (2026-07-01, enterprise-wecom-v0.0.6-rc1)

RC1 tag `enterprise-wecom-v0.0.6-rc1` at `b0763ce`. Quick 5-item smoke test:

| # | Test | Result |
|---|------|--------|
| 1 | `我是谁` → identity (朱春霖 + OA + org + 岗位) | ✅ |
| 2 | `帮我记一下…深圳出差` → `我刚才说我要去哪里？` → recalled | ✅ |
| 3 | `请长期记住：偏好简洁分点` → `你记得我的回复偏好吗？` → recalled | ✅ |
| 4 | `列一下当前可用的 MCP 工具` → 5 services listed | ✅ |
| 5 | `搜索深圳今天天气` → model asked "确认后立即执行" → user "确认" → tavily_search executed (confirmed=True) → weather results delivered via stream | ✅ |

Audit log: `confirmation_required (confirmed=False)` → `succeeded (confirmed=True)` —
full confirmation flow works. 0 SIGBUS, 0 stream timeouts, 0 content moderation errors.

**enterprise-wecom-v0.0.6-rc1 is ready for GA tagging.**

### MCP policy confirmed parameter behavior analysis (2026-07-01)

**User question**: Why does the model sometimes ask for confirmation before
calling risky/write tools, and sometimes skip confirmation and call with
`confirmed=true` directly? Is there a time-window mechanism?

**Answer**: No time-window mechanism. The policy (`mcp_policy.py`) is completely
stateless — each `evaluate()` call is evaluated independently based only on the
`confirmed` boolean parameter passed by the model's tool call.

**Policy code** (`mcp_policy.py` `evaluate()` method, lines 98-121):

```python
def evaluate(self, server_name, tool_name, *, confirmed=False):
    # ... deny/allowlist checks ...
    risk = self.risk_for(server_name, tool_name)  # read/write/risky
    needs_confirmation = tool in confirm_tools OR (require_confirmation AND risk in {"write","risky"})
    if needs_confirmation and not confirmed:
        return "confirm"  # confirmation_required
    return "allow"        # succeeded
```

**No state**: no cache, no TTL, no session memory, no "remember previous
confirmation". Each call is evaluated independently.

**The `confirmed` parameter is controlled by the model**: `call_mcp_tool` in
`tools.py` has a `confirmed` boolean (default `False`). The model decides what
to pass. If the model passes `confirmed=True`, the policy allows it immediately
(no confirmation_required). If the model passes `confirmed=False`, the policy
returns `confirmation_required`, and the model should then ask the user to
confirm, then call again with `confirmed=True`.

**Two-round comparison** (RC1 smoke test, 2026-07-01):

| Round | What the model did | Policy result | User asked? |
|-------|---------------------|--------------|-------------|
| Round 1 (item 5) | Model called `call_mcp_tool(confirmed=True)` directly | `succeeded` (policy allowed because confirmed=True) | ❌ No |
| Round 2 (retest) | Model called `call_mcp_tool(confirmed=False)` → got `confirmation_required` → model asked user "确认后我立即执行" → user said "确认" → model called `call_mcp_tool(confirmed=True)` | `confirmation_required` → `succeeded` | ✅ Yes |

**Root cause**: LLM behavior non-determinism. The model sometimes follows the
correct flow (confirmed=False first → ask user → confirmed=True after user
confirms), and sometimes skips the "ask user" step (directly confirmed=True).
This is a model prompt issue, not a system/gateway/policy bug.

**Policy is correct**: `confirmed=False` → `confirmation_required` (blocked) ✅;
`confirmed=True` → `succeeded` (allowed) ✅. The policy evaluates correctly
regardless of who set `confirmed=True`.

**Possible fixes for Codex to consider**:

1. **Prompt-level fix**: Add a system prompt / tool description constraint:
   "When calling risky/write tools, always call with confirmed=false first. If
   the tool returns confirmation_required, ask the user to confirm, wait for
   their response, then call again with confirmed=true. Never set
   confirmed=true yourself without a user's prior confirmation."

2. **Gateway-level fix (more reliable)**: On the first call to a risky/write
   tool in a turn, the gateway could **ignore the model's `confirmed=true` and
   force `confirmed=false`** for the first attempt, guaranteeing
   `confirmation_required` fires. The user then confirms, and the second call
   with `confirmed=true` is allowed. This removes the model's ability to skip
   confirmation.

3. **Session-level confirmation cache (user's hypothesis, currently non-existent)**:
   Implement a time-limited confirmation cache: once a tool is confirmed by
   the user, subsequent calls to the same tool within N seconds don't need
   re-confirmation. This would make the "first round skips confirmation"
   behavior a feature (if the user confirmed the same tool recently). But this
   requires new code — it does not exist today.

**Impact on GA**: Does not block GA. The policy, audit, and stream delivery
all work correctly. The inconsistency is in model behavior, not system
behavior. Codex to decide the fix approach.

### MCP confirmation hardening verification and rollback note (2026-07-01)

Mac mini verified `f03e983 Enforce session-backed MCP confirmations` through the
production `call_mcp_tool` entry point with a real local stdio MCP server. The
verification covered the intended hardening behavior: direct model-supplied
`confirmed=true` was blocked on the first attempt; a later same-session,
same-tool, same-argument call after user `确认` succeeded; changed arguments and
different sessions stayed blocked.

After that verification, the branch was intentionally rolled back to the
previous MCP policy behavior at the user's request. The rollback is a normal
revert commit on top of the verification record, so Mac mini can pull the branch
without rewriting history and re-run validation against the pre-hardening
behavior.

### Revert `f03e983` — VERIFIED + gateway restarted on reverted code (2026-07-01, commit 68d7b25)

`git fetch origin` + `git pull --ff-only origin enterprise/wecom-mcp-policy-audit`
→ `8c73a6d..68d7b25`, `git rev-parse --short HEAD` → **`68d7b25`** (matches
target). Branch history is `f03e983` (hardening) → `8c73a6d` (verification
record) → `68d7b25` (this revert); the ff-only pull advanced HEAD by exactly
the revert commit.

**Code revert confirmed.** `git show 68d7b25 --stat` reverts the 21 files
f03e983 touched. After pull:
- `MCPConfirmationGuard`, `confirmation_state_enabled`, `require_or_consume` →
  **no matches** in `contrib/agentseek-enterprise/src/` or the example src. The
  session-backed guard is fully removed.
- The three env vars (`AGENTSEEK_ENTERPRISE_MCP_CONFIRMATION_STATE_ENABLED` /
  `_TTL_SECONDS` / `_MAX_PENDING`) → **removed** from both `.env.example` files.
- `call_mcp_tool` signature returned to `(server_name, tool_name, arguments,
  confirmed=False)` — no `ToolRuntime` param — and the body once again calls
  `policy.evaluate(server_name, tool_name, confirmed=confirmed)` directly, with
  no preflight, no `effective_confirmed`, no pending-state lookup.
- `test_mcp_policy.py` shrank from 13 → **8 tests** (the 5 guard tests removed).

**Reverted unit suite**: `uv run --offline pytest
contrib/agentseek-enterprise/tests/test_mcp_policy.py -v` → **8 passed, 0.24s**.
No regression in policy evaluation, allowlist/denylist, confirmation-for-write,
audit redaction, or policy-file/env merging.

**Reverted production behavior** — drove the production `call_mcp_tool` against a
real local stdio MCP server with `AGENTSEEK_ENTERPRISE_MCP_CONFIRM_TOOLS=
verify/search` (a locally confirm-listed tool, per the task). Harness:
`/private/tmp/mcp_verify/run_revert_verify.py`. **6/6 PASS**:

| Check | Expected (reverted) | Observed |
|-------|---------------------|----------|
| setup | guard absent, `confirmation_state_enabled` attr gone | ✅ both absent |
| basic | `list_mcp_tools` connects, lists `verify/search` | ✅ |
| **R1** first `confirmed=true` on a confirm-listed tool | **executes directly** (the previously-problematic scenario, recovered) | ✅ `RESULTS for 'alpha'`; audit `succeeded confirmed=True` |
| R2 `confirmed=false` | still `confirmation_required` (policy still gates) | ✅ no execution; audit `confirmation_required confirmed=False` |
| R3 `confirmed=true` with fresh args | executes, no pending-state dependency | ✅ `RESULTS for 'gamma'` |

The audit tail confirms the restored stateless semantics — policy judges purely
by the `confirmed` argument:
```
succeeded              confirmed=True   args={'query':'alpha'}    # R1 — first confirmed=true executes (was blocked under f03e983)
confirmation_required  confirmed=False  args={'query':'beta'}     # R2 — confirmed=false gated
succeeded              confirmed=True   args={'query':'gamma'}    # R3 — fresh args, executes immediately
```
This is the pre-`f03e983` behavior restored: the model's `confirmed=true` is
again honored on the first attempt, and there is no session/tool/argument
pending store.

**Gateway restarted on the reverted code.** The previously-running gateway (pid
12844, started 18:16) still held `f03e983` in memory, so reverting the repo did
not change the live process. Per the user's instruction, it was restarted:
SIGTERM'd the `uv run` parent (12837) + gateway (12844); the DM sidecar child
(13267) exited with its parent; port `:12000` freed. Relaunched via
`bash examples/enterprise_wecom_digital_employee/scripts/run_gateway.sh` (the
canonical launcher; matches the prior command exactly). New gateway pid **21413**
(parent uv-run 21407). Boot log is clean (8 lines, no errors/traceback/SIGBUS):

```
INFO:  Started server process [21413]
INFO:  Application startup complete.
INFO:  Uvicorn running on http://0.0.0.0:12000
INFO:  schedule.start complete
INFO:  channel.manager started listening      (wecom + mcp.lifecycle + skills.lifecycle up)
Tavily MCP server running on stdio            (MCP child spawned)
```

`:12000` is LISTENING again. FlClash was OFF at restart time (healthy network
conditions — DM/WeCom egress direct), so the gateway came back without the
DM/WeCom-API connectivity failures documented elsewhere in these notes.

**Basic chain.** The revert's diff touches only MCP-confirmation code
(`mcp_policy.py`, `tools.py`, both `.env.example`, docs, tests) — identity,
short-term memory, explicit long-term store, and seekdb are byte-for-byte the
`v0.0.6-rc1` verified state, so no regression is possible from this revert in
those subsystems. The restarted gateway boots cleanly with all channels and the
MCP child (so `列一下当前可用的 MCP 工具` remains functional). A live WeCom
identity/short-term/seekdb turn was not separately driven (it needs a signed
WeCom callback), but is unaffected by the revert's scope.

**Net state.** HEAD = `68d7b25`; the live gateway is now serving the reverted
(stateless, `confirmed`-parameter-driven) MCP policy; the previously-problematic
scenario (model passing `confirmed=true` on the first attempt to a confirm-listed
tool) executes directly again, as it did before `f03e983`.

> **Handoff note for Mac Pro Codex (2026-07-02).** The session-backed
> confirmation hardening you implemented in `f03e983` was **rolled back at the
> user's request** (`68d7b25`). The revert is verified working end-to-end (live
> WeCom + audit proof below). The guard code (`MCPConfirmationGuard`,
> `confirmation_state_enabled`, the 3 env vars) is gone; `call_mcp_tool` once
> again evaluates purely on the model-supplied `confirmed` argument. If you
> re-introduce session-backed confirmation, the "audit before/after" section
> below shows the exact behavioral delta to reproduce. No code action is
> required from Codex unless the user asks to re-land the hardening.

### Live WeCom end-to-end verification — 熊积健 (2026-07-01 evening, all PASS)

After the gateway restart on reverted code (pid 21413), drove the full basic
chain live from the WeCom client as employee **熊积健** (OA `xiongjijian`,
identity: 公司总部 / 信息技术部 / 数智产品研发团队 / 软件开发岗兼数据开发岗):

| Step | Message | Result |
|------|---------|--------|
| 1. Identity | `我是谁` | ✅ full identity; DM sidecar spawned pid 25743 (cache miss), subsequent turns cache-hit |
| 2. Short-term store | `帮我记一下：明天要参加数据治理评审会` | ✅ "已记下：明天（2026-07-02）参加数据治理评审" |
| 3. Short-term recall | `我刚才说明天要做什么？` | ✅ "数据治理评审" |
| 4. Explicit long-term store | `请长期记住：我的汇报对象是 CTO` | ✅ persisted to postgres `langgraph_store_items` (confirmed in DB) |
| 5. Explicit long-term recall + boundary | `我的汇报对象是谁？` | ✅ "CTO"; no 评审会 mix-in |
| 6. Work-duty (employee_context + long-term) | `我的工作职责是什么？` | ✅ 岗位/部门/组织路径; after storing "数据开发和软件开发工作", recalled and labeled `（长期记忆）` |
| 7. MCP tool list | `你有哪些可用的mcp工具` | ✅ 5 services listed with correct risk labels |

**Memory backend in use** (confirmed via DB inspection, not log guessing):
short-term + explicit long-term both go to **PostgreSQL**
(`postgresql+psycopg://localhost/agentseek`,
`AGENTSEEK_ENTERPRISE_*_SQLALCHEMY_URL`). Tables: `enterprise_short_term_messages`
(short-term), `langgraph_store_items` (explicit long-term, `/employee-profile.md`
items). 熊积健's three work_context facts (评审会 / CTO / 数据开发+软件开发)
all persisted under namespace `hmac-b81a…` — correctly **isolated from 朱春霖**
(`hmac-8129…`). The SQLite files under `runtime/` are stale (Jun 30) because the
SQLAlchemy/Postgres path took over; they are not being written and that is
expected.

**Reverted MCP policy behavior — live.** Triggered `tavily-search/tavily_search`
(risky) via `搜索一下今天深圳的天气`:
- Model first called with `confirmed=false` → `confirmation_required`, replied
  "请确认是否执行此搜索？" (no execution).
- User sent `确认` → model called `confirmed=true` → **executed**, returned real
  Shenzhen weather data (深圳市气象局), replied at 19:26. No WeCom stream
  timeout (the `0488ede` placeholder fix held).

### Audit before/after — the revert smoking gun (same file, restart as divider)

`runtime/mcp-audit.jsonl` (PROJECT_ROOT-relative →
`examples/enterprise_wecom_digital_employee/runtime/mcp-audit.jsonl`). The
gateway restart at **18:58 (+08)** is the dividing line between the f03e983
gateway and the reverted gateway; both wrote to the same file, so the behavior
delta is directly visible:

| Window | Entries | `confirmed` | `reason` |
|--------|---------|-------------|----------|
| **Pre-restart, f03e983 gateway** (18:19–18:26 +08) | 8 × `confirmation_required` | all `False` | **GUARD**: `model-supplied confirmed=true ignored; latest user message…` / `pending employee confirmation registered` |
| **Post-restart, revert gateway** (19:24 +08) | `confirmation_required` | `False` | **clean**: `explicit confirmation required` |
| **Post-restart, revert gateway** (19:25 +08) | `succeeded` | **`True`** | **clean**: `allowed by policy` |
| **Post-restart, revert gateway** (22:17 +08) | 7 × `succeeded` (gildata read) | `False` | read tools execute directly |

**Decisive flip:** under `f03e983`, the model's `confirmed=true` first attempt
was overridden to `confirmed=false` and logged with a guard reason; under the
revert, `confirmed=true` executes and logs `succeeded … allowed by policy`. No
post-restart entry contains any guard reason string. `confirmed=true` ∧
`action=confirmation_required` violations: **0**. This is the definitive proof
the revert is live.

### seekdb write-side investigation (2026-07-01) — healthy; earlier "empty" claim was a wrong path

Initial observation "seekdb store is empty / not writing" was **wrong** — caused
by checking `examples/enterprise_wecom_digital_employee/runtime/contextseek`
(empty). The gateway process cwd is the **repo root** (run_gateway.sh does
`cd "$REPO_ROOT"`), and `AGENTSEEK_CTX_SEEKDB_PATH=./runtime/contextseek` is
relative, so it resolves to **`<repo-root>/runtime/contextseek`** (a live
**257 MB** store). Evidence it is healthy and writing:

- seekdb is an **OceanBase-based embedded engine** (`seekdb.log` =
  `ob_server.cpp` / `observer instance` / `multi tenant synced` / `schema ready`
  boot sequence). It runs **in-process** (`run/seekdb.pid` = the gateway pid);
  the `run/lua.sock` is its internal IPC socket. No separate server process/port.
- Booted cleanly at gateway start: `server_start 18/18 observer start success`.
- `store/sstable/block_file` (134 MB) was rewritten **19:14–19:20 +08**, i.e.
  during/right after the work-context memory turns.
- `seekdb.log`: **0** error/warn/fail/panic lines.
- ContextSeek `build_prompt`/`save_state` log **only on skip/error** (DEBUG), not
  on success — so the gateway log showing only `ContextSeek client initialized`
  is expected, not a sign of failure. No `skipped` lines appeared either.

**Path-resolution discrepancy worth remembering:** the seekdb path is
**cwd-relative** (repo-root) while the MCP audit path is **PROJECT_ROOT-relative**
(example dir) — different rules, which is why the seekdb check initially went to
the wrong directory while the audit check was always correct.

**Retrieve-side round-trip:** write-side is proven healthy. The work-duty recall
worked, but the reply labels it `（长期记忆）`, which maps to the **explicit
long-term store** (langgraph `employee-profile.md`), not unambiguously to seekdb
semantic. Isolating seekdb retrieve from the explicit store would need either a
temporary retrieve-hits log line + restart, or an offline probe on a copied store
(disruptive to the live gateway); not done. This is orthogonal to the revert
(revert does not touch ContextSeek) and does not block it.

### Overnight run health check (2026-07-02 morning) — clean

Reviewed the full ~14 h the gateway (pid 21413) ran unattended after the restart:

- **Uptime/stability:** pid 21413 ran 13 h 50 m, no restart (pid unchanged); DM
  sidecar pid 25743 stable 14 h (identity served from cache, no respawns).
- **Memory:** RSS ~870 MB, +~1 MB over 14 h → **no leak**.
- **Errors:** 0 SIGBUS / 0 traceback / 0 exit-138 / 0 OOM / 0 ERROR-level log /
  0 non-2xx HTTP (406 requests, all `200`). `seekdb.log`: 0 errors.
- **MCP audit (17 entries):** 9 `confirmation_required` (8 pre-restart guard +
  1 post-restart clean) + 8 `succeeded` (1 tavily `confirmed=true` + 7 gildata
  read `confirmed=false`); **0 `failed` / 0 `denied`**.
- **WeCom traffic:** 25 text messages (18:00 ×2 + 19:00 ×18 [the verification] +
  22:00 ×5 [a gildata finance query]); quiet overnight (last text 22:16, then
  normal stream/event polls). 2 `duplicate_msgid` (normal WeCom retry dedup).

**Post-check incident (resolved):** the background shell task that launched the
gateway was stopped by the harness, which took the gateway (21413), the `uv run`
parent, and the DM sidecar down with it (clean `Application shutdown complete`).
Restarted via `setsid` so the process is fully detached from the session and
won't be reaped again — new gateway pid **35923**, DM + WeCom API probed
directly reachable (`nc` to `192.10.50.26:5236` REACHABLE; `qyapi.weixin.qq.com`
HTTP 200). FlClash was ON at restart but in **system-proxy mode (TUN off)**
(default route via `en1`, not `utun`) — the healthy configuration; the new DM
sidecar spawns lazily on the first identity turn.

**Net verdict.** The `68d7b25` revert is verified live and stable: full basic
chain works for a fresh employee (熊积健), the MCP confirmation policy judges
purely by `confirmed`, the model's `confirmed=true` executes directly, and a 14 h
overnight run was clean. No action needed unless re-landing the hardening is
requested.

---

## Work item for Codex: durable-memory dedup + slot supersession (filed 2026-07-02)

**Owner:** Codex (Mac Pro) implements; Mac mini pulls, deploys, and tests against
the acceptance criteria + test plan below. Branch: open a new branch off
`enterprise/wecom-mcp-policy-audit` (do **not** commit to the revert branch's
history directly if the user prefers a reviewable PR — confirm with the user).

### Problem (with live evidence)

The durable employee-memory store accumulates **duplicate** and **contradictory**
records because writes are append-only with only exact-string dedup. Confirmed
against real data in postgres (`langgraph_store_items`). 朱春霖's
`/employee-profile.md` (namespace `…/hmac-8129…/filesystem`):

```
# Employee Memory
- [work_context] 朱春霖明天（2026-07-01）下午去北京出差      ← conflicts with below
- [preference] 企微回复偏好简洁、分点的回复方式               ┐
- [work_context] 明天（2026/7/1）下午去深圳出差               │ 3 near-duplicate
- [preference] 企微回复偏好：简洁、分点呈现                   │ preferences
- [work_context] 明天（2026-07-01）下午去深圳出差             │
- [preference] 偏好简洁、分点的回复方式                        ┘
- [work_context] 2026年7月2日下午去深圳出差
- [work_context] 负责数据架构工作
```

Two failure modes, both observed in production-style use:
1. **Duplicates** — same `preference` stored 3× with slightly different phrasing.
2. **Contradictions** — `work_context` about the same event ("明天出差") with
   different destinations (北京 vs 深圳) and dates (7/1 vs 7/2), all retained.

### Root cause (code)

All in [`contrib/agentseek-enterprise/src/agentseek_enterprise/long_term_memory.py`](../../contrib/agentseek-enterprise/src/agentseek_enterprise/long_term_memory.py).

- **Dedup is exact-line match only** — `remember_employee_memory` at
  [line 76](../../contrib/agentseek-enterprise/src/agentseek_enterprise/long_term_memory.py#L76):
  `if line in content: return "… already recorded."`. The three preferences
  differ in punctuation/phrasing ("回复方式" vs "呈现", with/without "企微回复"),
  so the exact-match misses and all three append.
- **No slot / supersession concept** — `category` is only `preference` |
  `work_context` ([line 58](../../contrib/agentseek-enterprise/src/agentseek_enterprise/long_term_memory.py#L58)).
  Two facts about the same temporal event ("明天去北京" then "明天去深圳") are
  treated as independent `work_context` lines; nothing recognizes the later one
  should supersede the earlier.
- **One free-text markdown blob per employee** — all memories live in a single
  `/employee-profile.md` string ([line 14](../../contrib/agentseek-enterprise/src/agentseek_enterprise/long_term_memory.py#L14));
  read-modify-write via `store.put`. No per-line metadata (timestamp, slot,
  superseded flag, valid-until).
- **Recall returns the raw accumulated blob** —
  `recall_employee_memory` ([line 36](../../contrib/agentseek-enterprise/src/agentseek_enterprise/long_term_memory.py#L36))
  hands the model the full markdown. The model already detects the duplicates at
  recall time ("目前此条偏好有多条重复记录，需要清理吗?") but the noisy prompt can
  make it waffle, and storage still grows.

### Current mechanism (for context)

- Tools: `recall_employee_memory`, `remember_employee_memory(memory, category)`,
  `forget_employee_memory(memory)`. Scoped per employee via
  `enterprise_filesystem_namespace(runtime)`; stored at `/employee-profile.md`.
- `_normalize_memory` collapses whitespace; `_contains_sensitive_marker` blocks
  credentials; `_MAX_MEMORY_CHARS=500`, `_MAX_PROFILE_CHARS=8000`.
- Backend: langgraph `BaseStore` → PostgreSQL (`langgraph_store_items`,
  `postgresql+psycopg://localhost/agentseek`). Short-term memory is a separate
  table (`enterprise_short_term_messages`) and is **out of scope** for this change.
- ContextSeek/seekdb is a **separate** semantic layer and is **out of scope**.

### Implementation scope for this iteration

**Do now: P0 (write-side semantic dedup) + P3 (read-side reconcile).** Both are
contained to `long_term_memory.py`, do not change storage schema, and directly
stop the bleeding. P1/P2 are documented below as the follow-on direction so the
design rationale is preserved — **do not implement P1/P2 in this iteration**
unless the user explicitly expands scope.

#### P0 — write-side semantic dedup (in `remember_employee_memory`)

Replace the exact-line dedup ([line 76](../../contrib/agentseek-enterprise/src/agentseek_enterprise/long_term_memory.py#L76))
with same-category fuzzy matching:

1. Parse existing bullets into `(category, text)` pairs from the markdown
   (`- [category] text`). Provide a small `_parse_profile(content)` helper.
2. Normalize for comparison: lowercase, strip CJK + ASCII punctuation
   (`，。、：；！？,.:!;()（）""''`), collapse whitespace. Keep this
   **comparison-only** — the stored line keeps its original wording.
3. Compute similarity against each existing bullet of the **same category**.
   For CJK, character-shingle similarity works better than word tokens. Use
   `difflib.SequenceMatcher(None, a, b).ratio()` **or** Jaccard over char
   2-grams/3-grams. Pick one; wrap behind a `_similar(a, b) -> bool` helper.
4. Threshold tunable via env, default ~0.70:
   `AGENTSEEK_ENTERPRISE_MEMORY_DEDUP_THRESHOLD` (0.0 disables → current
   exact-match behavior becomes a subset; 1.0 = exact only).
5. On match: **replace** the matched line in place with the new wording (so the
   latest phrasing wins, line count stays flat). Return a distinct confirmation,
   e.g. `"Updated an existing durable memory (was a near-duplicate)."` — do not
   silently no-op; the model should know it updated rather than appended.
6. On no match: append as today.
7. Keep the existing exact-match short-circuit as a fast path before fuzzy match.

Cross-category never dedupes (a `preference` and a `work_context` can coexist
even if textually similar).

#### P3 — read-side reconcile (in `recall_employee_memory`)

Before returning the `[DurableEmployeeMemory]` block, clean the content so the
prompt is not noisy (this also cleans **existing** dirty profiles like 朱春霖's
without a migration):

1. Parse bullets into `(category, text)`.
2. Within each category, collapse near-duplicate bullets (same `_similar` helper
   + threshold from P0) to **one** representative — prefer the latest (last in
   file = most recently written).
3. Return the deduped markdown (re-emit as `- [category] text` lines). Keep the
   `[DurableEmployeeMemory]` header and boundary prompt verbatim
   ([lines 48-53](../../contrib/agentseek-enterprise/src/agentseek_enterprise/long_term_memory.py#L48-L53)).
4. This is a **view** over stored data — do not mutate the store on recall.

> Note: P3 alone hides contradictions from the model but does not resolve the
> 北京/深圳 case (those are different strings, not near-duplicates). P1 (below)
> is the real fix for contradictions. P3's job is dedup of near-identical lines.

### Acceptance criteria (Codex must hit these; Mac mini tests against them)

1. Storing the exact same `(category, memory)` twice → still one line (current
   behavior preserved).
2. Storing the same preference with **different phrasing/punctuation**
   (the three real strings above) → ends as **one** line, latest wording.
3. Storing a `preference` and a textually-similar `work_context` → **both** kept
   (cross-category no-merge).
4. Sensitive content and size-cap refusals still fire (no regression).
5. `recall_employee_memory` on 朱春霖's current dirty profile returns ≤1 line per
   near-duplicate cluster (the 3 preferences collapse to 1).
6. `forget_employee_memory` still removes the intended line (no regression).
7. Threshold env var works: set to `1.0` → behaves as exact-match (backward
   compat); set to `0.0` → never merges.
8. No regression to identity / short-term memory / seekdb / MCP policy. The
   `68d7b25` revert behavior (MCP confirmation purely by `confirmed`) is
   **untouched** — this change must not edit `mcp_policy.py` or `tools.py`.

### Test plan Mac mini will execute (after pulling Codex's branch)

**A. Unit tests (Codex should add `test_long_term_memory.py`; Mac mini re-runs):**
- exact-duplicate → single line
- near-duplicate phrasings (the 3 real preference strings) → single line, latest wins
- cross-category similarity → both kept
- threshold=1.0 → exact-match behavior; threshold=0.0 → no merge
- sensitive/size refusals unchanged
- `recall` dedupes a fixture dirty profile to ≤1 per cluster
- `forget` removes the right line from a deduped profile

**B. Live WeCom (熊积健 + 朱春霖, on the restarted gateway):**
1. 朱春霖 `我的长期回复偏好是什么？` → reply cites a **single** preference (P3
   cleans the existing 3-way dup); no "多条重复记录" waffle.
2. As 熊积健, store the same preference 3× with different phrasings →
   `SELECT value_json::jsonb->>'content' FROM langgraph_store_items WHERE
   namespace_json LIKE '%hmac-b81a%'` shows **one** line (P0).
3. As 熊积健, store `[preference] X` and a textually-similar `[work_context] X`
   → both present (cross-category).
4. Identity / short-term store+recall / explicit long-term recall+boundary /
   MCP list / MCP confirm round-trip (tavily) — full basic chain, no regression.
5. Audit log still shows the reverted MCP behavior (clean reasons); memory
   change must not touch MCP audit.

**C. Regression sweep:** run
`uv run pytest contrib/agentseek-enterprise/tests -q` (expect all green,
including the new `test_long_term_memory.py`).

### Follow-on direction (documented, NOT this iteration)

- **P1 — slot-based supersession.** Add a `slot` parameter to
  `remember_employee_memory` (model fills it from semantics). Same slot + new
  value → replace. Examples: `pref.reply_style`, `travel_plan`,
  `meeting:2026-07-02`, `role`, `manager`. This is the real fix for the
  北京/深圳 contradiction (today's two-string mismatch is not near-duplicate, so
  P0/P3 won't catch it). Needs prompt guidance so the model emits `slot`.
- **P2 — structured per-memory storage.** Move from one markdown blob to one
  store item per memory (key = slot or uuid, value = `{category, content,
  created_at, superseded_by, valid_until}`); optionally `index=True` to get
  langgraph store vector search for free. Markdown becomes a rendered view. This
  is the clean terminal state but a larger refactor — do it after P0/P1 validate
  the direction.
- **P4 — periodic compaction/expiry.** Model-driven consolidation pass: drop
  stale temporal facts whose date passed, merge残余 dupes. Background or
  on-demand tool.
- **P5 — contradiction surfacing.** On write-time conflict (same slot, different
  value), confirm with the user or auto-supersede with a notice.

### Constraints / non-goals

- **Do not** touch `mcp_policy.py`, `tools.py`, or the MCP confirmation behavior
  — the `68d7b25` revert must stand.
- **Do not** migrate existing data; P3 must handle dirty profiles read-only.
- Keep the markdown line format backward-compatible enough that a profile
  written by old code is still readable (P3's parser must tolerate bullets
  without slot tags).
- Keep the change behind the threshold env so it can be disabled without a
  redeploy if it over-merges in production.
- Watch the write-path latency budget: P0's similarity check must be cheap
  (profile is capped at 8 KB → O(n) over a few dozen bullets is fine). Do **not**
  call an embedding model on every `remember` call; if embedding-based dedup is
  wanted later, gate it behind an env flag and run it async/out-of-band.

### Memory dedup verification (enterprise/memory-dedup, cdc6ea9) — PASS

Mac mini pulled `enterprise/memory-dedup` (`cdc6ea9 Implement durable memory
dedup`) and verified P0 (write-side near-duplicate dedup) + P3 (read-side
read-only reconcile) in the pre-production environment. Codex changed only
[`long_term_memory.py`](../../contrib/agentseek-enterprise/src/agentseek_enterprise/long_term_memory.py)
(+269/-56) and added
[`test_long_term_memory.py`](../../contrib/agentseek-enterprise/tests/test_long_term_memory.py)
(+250).

**Static / local checks**

- `git diff origin/enterprise/wecom-mcp-policy-audit -- mcp_policy.py tools.py`
  → **no output** (zero diff). The `68d7b25` revert and MCP confirmation behavior
  are untouched — the hard constraint held.
- `PYTHONPATH=contrib/agentseek-enterprise/src uv run pytest contrib/agentseek-enterprise/tests -q`
  → **53 passed**.
- `uv run ruff check --no-fix long_term_memory.py test_long_term_memory.py`
  → **All checks passed!** (Full-repo ruff not used as a blocker — pre-existing
  issues in `mcp_policy.py`, out of scope.)

**Implementation review (matches spec)**

- P0: exact-match short-circuit → `_find_near_duplicate_line` (reverse iter,
  same-category + `_similar`) → `_replace_durable_memory` replaces in place,
  returns `"Updated an existing durable memory (near-duplicate)."`
- P3: `_recall_employee_memory` returns `_deduped_profile_view(content)` — a
  read-only view, store not mutated; header/boundary prompt verbatim.
- Similarity: normalize (strip CJK/ASCII punct, lowercase, drop container words
  like 企微回复偏好/回复方式) → char 2-shingle Jaccard; threshold env
  `AGENTSEEK_ENTERPRISE_MEMORY_DEDUP_THRESHOLD` default 0.70; `<=0` disables,
  `>=1` degrades to exact match.

**Deterministic proof (fake store + the exact spec strings — stronger than
relying on model behavior):**

| Check | Input | Result |
|-------|-------|--------|
| P0 | store the 3 synonymous preferences in sequence | 3 → **1 line**, latest wording wins; returns `recorded` → `Updated near-duplicate` → `Updated near-duplicate` |
| C | same text stored as `preference` + `work_context` | **both kept** (no cross-category merge) |
| D | threshold=0.0, store the 3 | **3 kept** (dedup disabled) |
| threshold=1.0 | the 3 different phrasings | **3 kept** (exact-match mode) |
| exact-dup | identical string twice | 2nd → `"already recorded."` (backward compat) |
| P3 (real data) | 朱春霖's stored profile (3 preferences) fed to `_deduped_profile_view` | **3 → 1** in the recall view (latest wording `偏好简洁、分点的回复方式`); the 4 conflicting 北京/深圳 travel entries correctly **not** merged (different strings = not near-duplicates; contradiction resolution is P1, out of scope) |

**Live WeCom (gateway restarted on the branch, pid 51536, clean boot, FlClash
system-proxy/TUN-off, DM sidecar cold-spawned pid 53420):**

- **Test A — 朱春霖 `我的长期回复偏好是什么？`** → replied with a **single**
  preference (简洁 + 分点呈现 as one preference's two aspects), **no** 3-way
  duplicate listing, **no** 出差 mix-in, and **no** "多条重复记录，需要清理?"
  waffle (which the model emitted pre-fix). P3 confirmed end-to-end through the
  model.
- **Identity regression** — 朱春霖 `我是谁` → full identity (团队长兼数据架构师 /
  公司总部·信息技术部·数智产品研发团队 / 员工ID), cache hit after the cold
  sidecar spawn. 0 SIGBUS.
- **MCP regression** — `列一下当前可用的 MCP 工具` → 5 services listed with
  correct risk labels (tavily `⚠️ risky 需确认`, agent-platform write, gildata
  read). `mcp-audit.jsonl` still 17 lines (`list_mcp_tools` does not audit);
  **0 new guard-reason strings** (the 8 historical guard entries are all
  pre-`68d7b25`-restart — the revert's clean-reason behavior is intact).

**Scope note.** Live B/C/D were not separately driven via WeCom because the
deterministic probe already exercises them with the **exact** spec strings (the
probe controls what is stored, so it is not subject to model phrasing
non-determinism). Short-term memory and seekdb are **not touched** by this
change (diff scope = `long_term_memory.py` only), so they were not re-driven
live; seekdb write-side was confirmed healthy on the parent branch.

**Verdict.** **P0 and P3 PASS** — near-duplicates collapse on write and on
recall, latest wording wins, cross-category never merges, threshold is
reversible (0.0 / 1.0), backward-compatible with exact-duplicate and old
slot-less bullets. **No over-merge or under-merge observed.** No regression to
identity / MCP / the `68d7b25` revert. P1 (slot supersession, the real fix for
the 北京/深圳 contradiction) and P2/P4/P5 remain documented as follow-on.

**Follow-on still open (not in this iteration):** the 北京/深圳 travel
contradiction is **not** resolved by P0/P3 (those entries are different strings,
correctly not merged) — it needs P1 (slot-based supersession). The local
PostgreSQL `trust` auth hardening is also still pending (see ops notes).

### Memory slot supersession verification (enterprise/memory-slots, 6dfe0b4) — PASS

Mac mini pulled `enterprise/memory-slots` (`6dfe0b4 Add durable memory slot
supersession`) on top of the verified P0+P3 baseline (`enterprise/memory-dedup`,
`73d17fe`). Codex changed only
[`long_term_memory.py`](../../contrib/agentseek-enterprise/src/agentseek_enterprise/long_term_memory.py)
(+127) and extended
[`test_long_term_memory.py`](../../contrib/agentseek-enterprise/tests/test_long_term_memory.py)
(+170). This delivers P1 (slot-based supersession) + P5 (contradiction
notification), which together resolve the 北京/深圳 contradiction that P0/P3
could not.

**Static / local**

- `git diff origin/enterprise/memory-dedup -- mcp_policy.py tools.py` → **no
  output** (zero diff). MCP confirmation / `68d7b25` revert untouched — hard
  constraint held for a second iteration.
- `pytest contrib/agentseek-enterprise/tests -q` → **60 passed** (was 53; +7
  slot/P5 tests).
- touched `ruff check --no-fix long_term_memory.py test_long_term_memory.py` →
  **All checks passed!**

**Implementation review (matches spec)**

- `remember_employee_memory` gained `slot: str | None = None`; the docstring
  guides the model with slot examples (`travel_plan`, `reply_style`, `manager`,
  `responsibility`, `meeting_plan`).
- Line format extended to `- [category|slot=<slot>] text`; old slot-less lines
  parse with `slot=None` (backward compatible). `_parse_header` only parses the
  slot when `_slot_supersession_enabled()` (env on).
- Supersession logic in `_remember_employee_memory`: exact-match short-circuit →
  if slot present, `_find_slot_line` (same category+slot) → if found, branch on
  `_similar`: **similar → P0 silent** (`Updated … near-duplicate.`); **not
  similar → P5** (`_replace_conflicting_slot_memory` → `已更新『<label>』: 之前
  记的是「<old>」, 现在改为「<new>」。`). Different slot → append; no slot →
  P0 near-dup (existing).
- `_find_near_duplicate_line` additionally requires `entry.slot is None`, so the
  slot path and the no-slot near-dup path cannot interfere.
- `_dedupe_entries` (P3) uses `_same_memory_bucket` (category + slot), so recall
  dedup respects slots.
- Env gate `AGENTSEEK_ENTERPRISE_MEMORY_SLOT_SUPERSESSION_ENABLED` default
  `true`; `0/false/no/off` disables → slot forced to None everywhere → pure
  P0/P3 behavior. `_SLOT_LABELS` maps `travel_plan`→出差计划 etc. for the notify
  message.

**Deterministic proof (fake store, exact spec strings — the P0-vs-P5 boundary
is the trickiest part and was stressed directly):**

| Check | Input | Result |
|-------|-------|--------|
| P1+P5 | same `travel_plan`, 北京 → 深圳 | 2nd returns `已更新『出差计划』: 之前记的是「明天去北京出差」, 现在改为「明天去深圳出差」。`; only 深圳 in store |
| P0 silent (via slot) | same `reply_style`, two near-dup phrasings | 2nd returns `Updated an existing durable memory (near-duplicate).` (NOT P5); 1 line, latest wording |
| diff slots | `travel_plan` + `meeting_plan` | both kept (2 lines) |
| env=false | same `travel_plan`, 北京 → 深圳 | both kept, **no slot tags**, no P5 → falls back to P0/P3 |
| P3 respects slot | two near-dup entries in same slot | recall view collapses to 1 |

**Live WeCom (gateway restarted on the branch, pid 68466, clean boot, slot env
default on, FlClash system-proxy/TUN-off, DM sidecar cold-spawned):**

- **Test A — 熊积健 P1+P5.** `请记住我明天去北京出差` → `请记住我明天去深圳出差`.
  The model filled **`slot=travel_plan`** for both (postgres after step 1:
  `- [work_context|slot=travel_plan] 2026-07-03 去北京出差`). Step 2 reply
  surfaced the contradiction: *"已更新：明天（2026-07-03）去深圳出差。同时提醒一下：
  你之前让我记住的是"明天去北京出差"，这次按"深圳"覆盖更新了。如果这两天分别要去
  北京和深圳，或是我改错了，告诉我一声。"* Postgres after step 2: **only 深圳**
  (`- [work_context|slot=travel_plan] 2026-07-03 去深圳出差`); 北京 superseded.
  The model paraphrased the tool's P5 return into a conversational notice but
  preserved the old→new semantics and offered to handle the two-trip case.
- **Test B — 熊积健 P0 silent (the boundary complement).** Two near-duplicate
  preferences with the same slot (`请记住我的企微回复偏好简洁、分点的回复方式` then
  `…简洁、分点呈现`). Postgres: **1 line** `- [preference|slot=reply_style]
  企微回复偏好：简洁、分点呈现` (latest wording). **No P5 contradiction
  notice** in either reply (correct: same-slot near-duplicate is a silent
  update, not a contradiction). The P0-silent-vs-P5-notify distinction is
  confirmed end-to-end.
- **Test E — 朱春霖 legacy backward compat.** `我的长期回复偏好是什么？` on the
  old slot-less profile → single preference (`简洁、分点呈现`), the model noting
  "目前偏好记录仅一条，无冗余". The slot-aware parser handles old slot-less
  lines with **no errors**; P3 dedup still collapses the legacy 3-way duplicate
  on recall.
- **Regression / health.** Gateway served all live turns across two employees
  (identity via DM sidecar, short-term, long-term recall) with **0 SIGBUS /
  0 traceback**. `mcp-audit.jsonl` still 17 lines, **guard-reason count still 8
  (all historical, pre-restart)** — the `68d7b25` revert's clean-reason behavior
  is intact. MCP code is byte-identical to the verified baseline (zero diff),
  so MCP list / tavily confirm / audit are structurally unaffected.

**Scope notes.** Live C (different slots) and D (env=false) were not separately
driven via WeCom because the deterministic probe already exercises them with
the exact spec strings (the probe controls the slot/text, so it is not subject
to model phrasing). seekdb and short-term memory are not touched by this change
(diff = `long_term_memory.py` only) and were not re-driven live.

**Verdict.** **P1 and P5 PASS** — slot-based supersession works, the
北京/深圳 contradiction is resolved (same slot, different value → supersede +
notify the user; same slot, near-duplicate → silent update), different slots
coexist, the feature is reversible via env, and old slot-less data recalls
correctly. **No over- or under-supersession observed.** No regression to P0/P3,
identity, MCP, or the `68d7b25` revert.

**Known limitations (carry forward, not blockers):** legacy slot-less data is
not retroactively slotted (P1 applies only to new slot-tagged writes); the
model is relied upon to fill consistent slots (no server-side slot
normalization). The local PostgreSQL `trust` auth hardening remains pending.

### v0.0.6 RC2 memory-slots tag (2026-07-02)

Published the memory-slot RC after Mac mini validated `enterprise/memory-slots`:

- RC tag: `enterprise-wecom-v0.0.6-rc2-memory-slots`
- RC tag commit: tag target
- Runtime implementation commit: `6dfe0b4`
- Verification commit: `5ec346c`
- Branch: `enterprise/memory-slots`

This RC carries forward the v0.0.6 MCP policy/audit and WeCom stream fixes, then
adds durable employee memory slot supersession:

- P0: write-side near-duplicate deduplication.
- P3: read-only recall cleanup for old dirty profiles.
- P1/P5: same `category + slot` with a different value supersedes the old value
  and returns an old-to-new notice; same slot with near-duplicate text remains a
  silent P0 update.

Mac mini verified the critical live case with 熊积健:

```text
请记住我明天去北京出差
请记住我明天去深圳出差
```

The model supplied `slot=travel_plan`; the second write replaced 北京 with
深圳, persisted only the new travel plan, and surfaced the old-to-new notice to
the employee. A separate near-duplicate reply-style test stayed silent, proving
the P0/P5 boundary.

The hard constraints remained intact:

- `mcp_policy.py` and the example `tools.py` have zero diff from the verified
  P0/P3 baseline.
- The `68d7b25` MCP confirmation revert remains in effect.
- `AGENTSEEK_ENTERPRISE_MEMORY_SLOT_SUPERSESSION_ENABLED=false` rolls back to
  the P0/P3 behavior.

This is an RC, not a GA freeze. Keep `enterprise-wecom-v0.0.5-ga-20260630` as
the immutable GA deployment tag until v0.0.6 receives a GA tag.

### Final branch smoke verification (enterprise/wecom-mcp-policy-audit, 2c626ce) — BLOCKER (do NOT promote to GA)

Mac mini pulled the consolidated final branch `enterprise/wecom-mcp-policy-audit`
@ `2c626ce` (RC tag `enterprise-wecom-v0.0.6-rc2-memory-slots` → `2c626ce`,
confirmed). The branch is a clean linear FF of the three prior branches
(revert + P0/P3 + P1/P5). Static checks pass and most live smoke passes, **but a
concurrency bug in the durable-memory write path surfaced during live smoke —
this is a GA blocker.**

**Static — all PASS**

- MCP hard constraint: `git diff 68d7b25 -- mcp_policy.py tools.py` → **no
  output** (zero diff). The `68d7b25` revert is intact; `f03e983` hardening
  symbols (`MCPConfirmationGuard`, `confirmation_state_enabled`,
  `require_or_consume`) are absent from src — **no guard revival**.
- `pytest contrib/agentseek-enterprise/tests -q` → **60 passed**.
- touched `ruff` on `long_term_memory.py` + `test_long_term_memory.py` →
  **All checks passed!**

**Live smoke — A/B/C and D-step1 PASS, D-step2 FAILS (blocker)**

Gateway restarted on `2c626ce` (pid 97596, clean boot, slot env default on,
FlClash system-proxy/TUN-off, DM sidecar cold-spawned pid 98302). All single
memory-tool-call turns worked:

- **A identity** — `我是谁` (朱春霖) → full identity. ✅
- **B short-term** — `帮我记一下…深圳出差` → `我刚才说我要去哪里？` recalled
  "明天（7/3）下午去深圳出差 ✈️" via conversational context. ✅ (Note: the model
  durable-stored "记一下" — said "已长期记住" — a prompt nuance, not a bug; recall
  was correct and source-labelled.)
- **C long-term recall (P3)** — `我的长期回复偏好是什么？` → single preference
  ("简洁、分点呈现"), model noting "目前偏好记录仅一条，无冗余". P3 dedup intact. ✅
- **D step1 P5 (single call)** — `请记住我明天去北京出差` (朱春霖) → P5 fired,
  superseded the travel_plan=深圳 (set in step B) with 北京; postgres confirmed
  `- [work_context|slot=travel_plan] 明天（2026-07-03）去北京出差`; reply surfaced
  the replacement. ✅

**D step2 — FAIL (the blocker).** `请记住我明天去深圳出差` (朱春霖). The model
issued **two parallel `forget_employee_memory` calls in one turn** (attempting to
clear multiple legacy 深圳 entries). Both calls do a non-atomic read-modify-write
on the shared `/employee-profile.md`; the two concurrent `store.put` operations
raced and the second hit a primary-key violation, crashing the whole turn. The
user received an error reply and the 深圳 update did **not** persist (postgres
still shows travel_plan=北京).

Log fragment (gateway_final.log, 2026-07-02 12:42:14):

```
PregelExecutableTask(name='tools', input=[{'name': 'forget_employee_memory', ...}, {'name': 'forget_employee_memory', ...}])
→ IntegrityError('(psycopg.errors.UniqueViolation) duplicate key value violates unique constraint "langgraph_store_items_pkey"')
  DETAIL: Key (namespace_json, item_key)=(["enterprise","v1","hmac-9b99…","hmac-8129…","filesystem"], /employee-profile.md) already exists.
  [SQL: INSERT INTO langgraph_store_items (namespace_json, item_key, value_json, created_at, updated_at) VALUES (%(namespace_json)s, %(item_key)s, %(value_json)s, %(created_at)s, %(updated_at)s)]
```

**Root cause.** The durable-memory tools
(`_remember_employee_memory`/`_forget_employee_memory`) perform a non-atomic
**read → modify → `store.put`** on the single `/employee-profile.md` row. When
the model makes **≥2 memory tool calls in one turn** (here: 2 parallel
`forget_employee_memory`), the calls race: both read the same profile, both
`put`. The langgraph PostgreSQL store's `put` on this path issues a plain
`INSERT … VALUES` (no `ON CONFLICT`/upsert clause), so when two concurrent puts
both believe the row is new, the second violates the
`langgraph_store_items_pkey` unique constraint → `IntegrityError` → the turn's
transaction aborts. The slot/P5/P0/P3 logic itself is correct (deterministic
probe + every single-call live turn passed); the defect is the **write-path
concurrency**, which is independent of the slot logic but blocks production.

**Impact.** Any turn in which the model makes multiple memory tool calls in
parallel can fail loudly (error reply + lost write). The model does this in
practice when cleaning up duplicates or storing related facts — exactly the
"forget the old duplicates" behavior the new dedup/slot features encourage.
Reproducible on this build.

**Fix direction for Codex (pick one, recommend #1):**

1. **Serialize per-profile writes** — wrap the `get → modify → put` sequence in
   `_remember_employee_memory`/`_forget_employee_memory` in a per-namespace
   `asyncio.Lock` (keyed by `enterprise_filesystem_namespace(runtime)`) so
   parallel tool calls in one turn queue instead of race. Most robust, fully in
   app control.
2. **Make the write idempotent on conflict** — catch `IntegrityError` from
   `store.put` and retry once as an update (re-read, re-apply, re-put).
3. **Drive the store to upsert** — ensure the langgraph store `put` uses
   `INSERT … ON CONFLICT (namespace_json, item_key) DO UPDATE` (may require a
   store config or version bump; verify the store backend).

Add a regression test that fires ≥2 `forget`/`remember` calls concurrently on
the same profile (via the real PostgreSQL store, not the fake) and asserts no
`UniqueViolation` and a consistent final profile.

**Recommendation.** **Do NOT promote `enterprise-wecom-v0.0.6-rc2-memory-slots`
to GA.** Keep `enterprise-wecom-v0.0.5-ga-20260630` as the immutable GA tag.
Route back to Codex for the write-path concurrency fix + a concurrent-write
regression test; re-run this smoke (especially a multi-parallel-memory-call
turn) before re-assessing GA. The MCP revert, P0/P3, and P1/P5 logic are
verified sound; only the concurrent-write defect blocks GA.

**Health (despite the blocker).** The gateway did not crash — the error failed
the single turn (pid 97596 still alive, :12000 listening). 0 SIGBUS / 0
exit-138. The blocker is isolated to parallel durable-memory writes; identity,
short-term, long-term recall, and (by zero-diff) MCP remain functional. E/F/G
of the smoke were not pursued further once the blocker was characterized.

### Durable memory concurrent-write fix (2026-07-02, pending Mac mini verification)

Codex fixed the blocker found in the final branch smoke test by serializing
durable employee profile writes per authenticated employee namespace.

**Fix.** `long_term_memory.py` now wraps the full `get -> modify -> put` section
in `_remember_employee_memory` and `_forget_employee_memory` with a
per-namespace `threading.RLock`. The tools are synchronous and can run in
parallel worker threads during one model turn, so a thread lock matches the
runtime execution mode. The lock key is the existing
`enterprise_filesystem_namespace(runtime)`, which means different employees can
still write concurrently, while one employee's `/employee-profile.md` blob is
updated one operation at a time.

This protects the single-gateway deployment mode validated on Mac mini. If the
gateway is later scaled to multiple OS processes or hosts, add a database-level
upsert/advisory-lock strategy before treating durable memory writes as
cross-process safe.

**Regression tests added.**

- Two concurrent `forget_employee_memory` calls against the same profile remove
  both target lines and do not lose either update.
- Two concurrent `remember_employee_memory` calls against the same profile store
  both facts and do not let the last writer clobber the first.

**Local verification after the fix.**

- `uv run pytest contrib/agentseek-enterprise/tests/test_long_term_memory.py -q`
  -> **18 passed**.
- `PYTHONPATH=contrib/agentseek-enterprise/src uv run pytest contrib/agentseek-enterprise/tests -q`
  -> **62 passed**.
- touched `ruff check --no-fix long_term_memory.py test_long_term_memory.py`
  -> **All checks passed!**

**Status.** Mac mini live verification is still required before GA. Re-run the
previous failing D-step2 (`请记住我明天去深圳出差`) and confirm the model can make
parallel memory calls without `UniqueViolation`, with the final profile showing
the latest `travel_plan=深圳`.

### D-step2 re-verification after concurrency fix (7b442a5) — PASS, blocker resolved

Mac mini pulled `7b442a5 Serialize durable memory profile writes` (per-namespace
`threading.RLock` around the full get→modify→put in `_remember_employee_memory`
and `_forget_employee_memory`; +2 concurrent regression tests). The exact
scenario that blocked GA was re-run and now passes.

**Static — PASS.** MCP hard constraint `git diff 68d7b25 -- mcp_policy.py
tools.py` → no output (revert intact); `pytest contrib/agentseek-enterprise/tests
-q` → **62 passed** (+2 concurrent); touched `ruff` → All checks passed.

**Fix review.** The `RLock` is acquired by namespace
([long_term_memory.py:148](../../contrib/agentseek-enterprise/src/agentseek_enterprise/long_term_memory.py#L148)
and
[:210](../../contrib/agentseek-enterprise/src/agentseek_enterprise/long_term_memory.py#L210))
and wraps the **entire** `store.get → modify → store.put` (not just the put),
which is what makes the read-modify-write atomic per profile. Per-namespace
granularity → different employees don't block each other. Root cause of the
original crash confirmed independently: `SQLAlchemyStore._put_item` issues a
plain `table.insert().values(...)` with no `ON CONFLICT`, so two concurrent puts
to the same `(namespace, item_key)` race → `UniqueViolation`.

**Deterministic proof against the REAL PostgreSQL store (not the fake):**
spawned a real `SQLAlchemyStore` against `postgresql+psycopg://localhost/agentseek`
and fired 2 concurrent ops on the same profile via `ThreadPoolExecutor`:

| Variant | Result |
|---------|--------|
| **Lock ON** (`7b442a5`) — 2 concurrent `forget` | both return `…removed.`; both targets deleted; **no exception, no lost update** |
| **Lock ON** — 2 concurrent `remember` (different facts) | both facts retained |
| **Lock OFF (control)** — 2 concurrent `forget` + 100 ms put delay | `IntegrityError: UniqueViolation` (the original error reproduced) **and** a lost update (one target still present) |

The control reproduces the original D-step2 failure precisely; the lock-on run
passes. This proves both the fix and that the probe actually catches the
regression (not a false pass).

**Live WeCom (gateway restarted on `7b442a5`, pid 23736, clean boot, FlClash
system-proxy/TUN-off) — the previously-failing message:**

- 朱春霖 `请记住我明天去深圳出差` (travel_plan was `北京` from the earlier
  successful D-step1):
  - **`UniqueViolation` / "An error occurred at stage" count: 0** — the turn no
    longer crashes (previously it always crashed here).
  - Reply surfaced the P5 supersession: *"已更新，朱春霖：明天（7/3）去深圳出差
    ✈️（之前的北京出差记录已替换为本条）"*.
  - postgres: `- [work_context|slot=travel_plan] 明天（7/3）去深圳出差` — 北京
    superseded by 深圳. ✅

**Verdict.** **The GA blocker is resolved.** The concurrent durable-memory write
defect (UniqueViolation + lost update on parallel memory tool calls within one
turn) is fixed by per-namespace write serialization, verified by a deterministic
real-PostgreSQL concurrency test (with a lock-off control that reproduces the
original failure) and by the live re-run of the exact failing message. MCP
revert, P0/P3, and P1/P5 remain sound (unchanged by the lock, which only affects
the memory write path).

**GA re-assessment: eligible.** With the blocker resolved and the full chain
statically + live verified, `enterprise/wecom-mcp-policy-audit` @ `7b442a5`
(supersedes RC tag `enterprise-wecom-v0.0.6-rc2-memory-slots`) is **eligible for
GA promotion** at the user's discretion. Two carry-over caveats (non-blockers):

1. **Single-process scope.** The `threading.RLock` serializes writes within one
   gateway process only. The current deployment is a single gateway process, so
   this is sufficient. If the gateway is ever scaled to multiple
   processes/machines, replace/augment with a DB-level mechanism (Postgres
   advisory lock, or drive `SQLAlchemyStore._put_item` to a real
   `INSERT … ON CONFLICT DO UPDATE` upsert). Codex documented this.
2. **Legacy slot-less data** is still not retroactively slotted/cleaned (P4
   compaction not done); `AGENTSEEK_ENTERPRISE_MEMORY_SLOT_SUPERSESSION_ENABLED=false`
   rolls back to P0/P3 if ever needed.

**Health.** Gateway pid 23736 stable, :12000 listening, 0 SIGBUS / 0 traceback.
The fix is isolated to the durable-memory write path; identity, short-term, and
MCP remain unaffected (MCP zero-diff confirmed).

### v0.0.6 GA tag (2026-07-02)

Published the v0.0.6 GA baseline after Mac mini re-ran the blocker smoke test
and confirmed that `7b442a5` resolves the durable-memory concurrent-write
failure:

- GA tag: `enterprise-wecom-v0.0.6-ga-20260702`
- GA tag commit: tag target
- Final verified integration commit: `60d0155`
- Runtime concurrency fix commit: `7b442a5`
- Superseded RC tag: `enterprise-wecom-v0.0.6-rc2-memory-slots` -> `2c626ce`
- Previous GA tag: `enterprise-wecom-v0.0.5-ga-20260630` -> `5cce3a2`
- Production branch: `enterprise/wecom-mcp-policy-audit`

This GA includes:

- MCP policy/audit and the preserved `68d7b25` MCP confirmation revert.
- WeCom stream placeholder delivery for slow confirmed tool calls.
- Durable memory P0/P3 near-duplicate dedup and recall cleanup.
- Durable memory P1/P5 slot supersession and contradiction notice.
- Durable memory per-employee profile write serialization for same-turn
  parallel memory tool calls.

Final verification highlights:

- MCP hard constraint: `mcp_policy.py` and the example `tools.py` are still
  zero-diff from `68d7b25`.
- Local enterprise regression: **62 passed**.
- touched `ruff` on durable-memory code/tests: **All checks passed!**
- Real PostgreSQL concurrency probe: lock-on passes for concurrent forget and
  remember; lock-off reproduces the original `UniqueViolation`.
- Live WeCom D-step2 re-run: P5 北京 -> 深圳 notice delivered, no
  `UniqueViolation`, and postgres shows `travel_plan=深圳`.
- Gateway health: pid 23736 stable, :12000 listening, 0 SIGBUS / 0 traceback.

Carry-over caveats:

- The profile write lock is process-local. The current deployment uses one
  gateway process. Multi-process or multi-host scaling needs a database-level
  advisory lock or true store upsert.
- Legacy slot-less memories are not retroactively slotted or compacted. P4
  compaction remains a future task.

**Verdict.** `enterprise-wecom-v0.0.6-ga-20260702` is the new immutable GA
deployment baseline. Keep `enterprise-wecom-v0.0.5-ga-20260630` for rollback
and audit history.

## v0.0.7 pgvector + PostgreSQL scram verification (enterprise/v0.0.7-pgvector, b9d6f2d) — production PASS, 2 test artifacts

Mac mini verified `enterprise/v0.0.7-pgvector` @ `b9d6f2d`: ContextSeek
semantic memory migrated from local seekdb to **PostgreSQL + pgvector**
(bge-m3 dense 1024-dim via ONNX/onnxruntime), and PostgreSQL auth switched
**trust → scram-sha-256** with a dedicated least-privilege app role. No
`mcp_policy.py`/`tools.py` changes; no seekdb data migrated.

**PostgreSQL + pgvector**

- Version **PostgreSQL 17.10 (Homebrew)**; db `agentseek`; pgvector **0.8.4**
  (built from source against `postgresql@17`, `CREATE EXTENSION vector`).
- `password_encryption = scram-sha-256`; `ssl = off` (loopback only).

**Auth — scram fully switched + verified.** All 6 active `pg_hba.conf` rules
(local + host 127.0.0.1/::1 + replication) changed `trust → scram-sha-256`
(backup at `pg_hba.conf.trust.bak.*`); `pg_reload_conf()` applied. Created
least-privilege role **`agentseek_app`** (non-superuser; `CONNECT` on db,
`USAGE/CREATE` on `public`, table/sequence privileges, `ALTER DEFAULT
PRIVILEGES`) — the gateway no longer runs as the `sambazhu` superuser.
Verification: no-password TCP → `fe_sendauth: no password supplied` (rejected);
`agentseek_app` + `sambazhu` with passwords → login OK. `.env` SQLAlchemy URLs
(MEMORY/STORE) and `AGENTSEEK_CTX_PGVECTOR_URL` carry `agentseek_app:<pw>@…`
(`.env` stays gitignored; password never committed/printed).

**bge-m3 ONNX.** `BAAI/bge-m3` dense, **1024-dim**, loaded via `onnxruntime` +
`tokenizers` (no torch in the gateway process). Used the
`Xenova/bge-m3` community ONNX export `onnx/model_quantized.onnx` (int8, 570 MB)
→ `./models/bge-m3-onnx/model.onnx` + `tokenizer.json` (downloaded via HF over
the 7890 proxy with resume; HF direct was rate-limited). Embedder self-test:
1024-dim, L2 norm = 1.0. **Note:** this is the int8-quantized variant — fine
for verification; for production quality consider the fp32/fp16 ONNX variant.

**`prod_check.py`** (Codex updated): `ContextSeek storage backend is pgvector` ✅,
`pgvector dims 1024` ✅, bge-m3 model+tokenizer paths exist ✅, short-term +
explicit durable memory use SQLAlchemy URL ✅ — all green.

**Live WeCom** (gateway restarted on pgvector, pid 13692, clean boot,
**0 `seekdb has opened`**, 0 SIGBUS):

- **A identity** — `我是谁` (朱春霖) → full identity; confirms the gateway's
  scram `agentseek_app` PG connection works (identity cache stored). ✅
- **D pgvector semantic** — `请长期记住：…负责数据架构工作` →
  `ContextSeek pgvector client initialized` (bge-m3 loaded, no seekdb);
  `contextseek_pgvector_items` table created (owner `agentseek_app`); semantic
  row stored under 朱春霖's scope. Then `我的工作职责是什么？` → recalled
  "**负责数据架构工作**" via pgvector cosine ANN. ✅
- **E isolation** — 熊积健 `我的工作职责是什么？` → answered from his OWN
  memory/context; **朱春霖's "数据架构工作" did not leak** (pgvector scope
  WHERE; 熊积健's scope has 0 rows with 数据架构). ✅
- **F MCP** — `列一下当前可用的 MCP 工具` → 5 services, risk labels correct
  (tavily ⚠️ 需确认). `mcp-audit.jsonl` still 17 lines, guard-reason count
  unchanged at 8 (historical) → `68d7b25` MCP revert intact. ✅
- B (short-term) / C (explicit long-term recall) share the same scram
  SQLAlchemy connection A proved; code unchanged from v0.0.6 GA.

**Static.** `git diff 68d7b25 -- mcp_policy.py tools.py` → **zero diff** (MCP
revert intact). `pytest contrib/agentseek-enterprise/tests -q` → **62 passed**.
contextseek `pytest` → 42 passed, 1 failed, 1 skipped (the skip = real-pgvector
integration, which was run explicitly with `AGENTSEEK_CTX_PGVECTOR_TEST_URL` →
**PASS**, confirming production code; see artifacts below).

**Two test artifacts (NOT production bugs — production verified via real-DB +
live). For Codex to clean up:**

1. `test_pgvector_add_retrieve_roundtrip` (unit, FakeEmbedder) **FAILs**:
   `assert hit.item.tags == ["t"]` got `[]`. Root cause: the test's
   `FakePgVectorDatabase` stores the psycopg `Jsonb(["t"])` adapter object
   as-is (never round-tripped through postgres jsonb), and `_tags_from_row` only
   handles `list`/`str` → returns `[]`. Real DB round-trip confirmed correct
   (psycopg deserializes jsonb → Python list). Fix: the fake should adapt
   `Jsonb` → value on store, or `_tags_from_row` should handle the adapter.
2. `_delete_real_rows` test helper calls `psycopg.connect(settings.url)`
   directly, bypassing `_psycopg_url` → fails if the URL is the SQLAlchemy-style
   `postgresql+psycopg://…` (the production `_connect()` path uses
   `_psycopg_url` and is correct; the real integration test PASSES with a plain
   `postgresql://` URL). Fix: use `_psycopg_url(settings.url)` in the helper.

**Operational note (handled).** During the real-test run, passing the
`postgresql+psycopg://…` URL to the test helper triggered a psycopg parse error
that echoed the URL including the `agentseek_app` password. The password was
**rotated immediately** (new random, role + `.env` updated); no other exposure.
Recommend Codex's helper fix (#2) to prevent recurrence.

**Verdict — RC1 eligible.** The v0.0.7 production functionality (pgvector
semantic memory with bge-m3 ONNX + scram auth + least-privilege role) is
verified end-to-end (real-DB integration + live store/recall/isolation), MCP
revert intact, 0 SIGBUS, seekdb fully replaced (0 `seekdb has opened`).
**Recommend tagging `enterprise-wecom-v0.0.7-rc1`** after Codex fixes the 2
test artifacts so the suite is green; the artifacts do not block production
behavior. Carry-over: consider fp32/fp16 bge-m3 ONNX for production embedding
quality; PG `ssl=off` is acceptable only while loopback-only.

## v0.0.7 RC1 re-verification (enterprise/v0.0.7-pgvector @ 0485453, tag enterprise-wecom-v0.0.7-rc1) — PASS, ready for GA

Mac mini re-verified RC1 after Codex's `0485453 test: harden pgvector
verification helpers` (fixes the two artifacts from the prior round: fake-DB
`Jsonb` tags handling in `_tags_from_row`, and `_psycopg_url` use in the test
helper to avoid the `postgresql+psycopg://` URL parse error + password echo).

**Branch/tag/commit.** `enterprise/v0.0.7-pgvector` @ `0485453`;
`enterprise-wecom-v0.0.7-rc1^{}` → `0485453` (match). Working tree clean (no
business code changes; `.env` not committed). RC1 delta vs the prior verified
`e882f3d`: `pgvector.py` (+6) + `test_pgvector.py` (+2/-1) — only the
pgvector/tags path and tests.

**Static + tests — all green, both artifacts fixed.**

- MCP hard constraint: `git diff 68d7b25 -- mcp_policy.py tools.py` → **zero
  output** (MCP confirmation revert intact).
- `pytest contrib/agentseek-contextseek/tests -q` → **43 passed, 1 skipped**
  (skip = real-pgvector integration, needs URL).
- `pytest contrib/agentseek-enterprise/tests -q` → **62 passed**.
- Real pgvector integration
  (`AGENTSEEK_CTX_PGVECTOR_TEST_URL=postgresql+psycopg://agentseek_app:…@…`,
  the `+psycopg` form that previously errored): **8 passed**, and the raw log
  contains **no password** — `_psycopg_url` fix verified, no leak.
- The previously-failing `test_pgvector_add_retrieve_roundtrip` now **PASSES**
  (`_tags_from_row` handles the `Jsonb` adapter).

**PostgreSQL scram — still fully in effect.** `password_encryption =
scram-sha-256`; pgvector `vector 0.8.4`; `agentseek_app` is non-superuser
(`rolsuper=f`); no-password TCP → `fe_sendauth: no password supplied`
(rejected); with-password → login OK. `.env` unchanged: `CTX_STORAGE_BACKEND=
pgvector`, `PGVECTOR_URL/TABLE/DIMS=1024`, bge-m3 ONNX+tokenizer paths present,
short-term + explicit-long-term SQLAlchemy URLs use `agentseek_app`.

**Live WeCom** (gateway restarted on RC1, pid 23132, clean boot):

- **A identity** — `我是谁` (朱春霖) → full identity; confirms the scram
  `agentseek_app` PG connection (identity cache stored). ✅
- **B pgvector semantic (the RC1-changed path)** —
  `请长期记住：…负责数据架构工作` → `我的工作职责是什么？` recalled
  "**负责数据架构工作**" via pgvector. ✅
- **F MCP** — `列一下当前可用的 MCP 工具` → 4/5 services listed with correct
  risk labels (tavily ⚠️ 需确认). `agent-platform` reported "连接失败，当前不
  可用" — that external SSE server (`172.20.16.242:8000`) is unreachable, **not
  a v0.0.7 regression** (gateway correctly reports it; other 4 servers fine).
- C (isolation) / D (short-term) / E (explicit long-term): not re-driven live
  this round — RC1's only change is the pgvector tags path, which the unit +
  real-pgvector integration tests cover, and these paths were live-verified on
  the identical config in the prior round (`e882f3d`).

**Health.** 0 SIGBUS / 0 traceback; 43 HTTP callbacks all `200`; audit
`guard-reason` count unchanged at 8 (historical) → MCP revert intact; seekdb
store `block_file` mtime frozen at 15:40 (pre-pgvector-switch) → **seekdb no
longer growing**, pgvector is the active semantic backend.

**Verdict — ready for GA.** RC1 fixes are verified (both test artifacts
resolved, suite green, no password leak), production functionality confirmed
live (identity + scram + pgvector semantic), MCP revert intact, 0 SIGBUS, seekdb
fully replaced. Recommend tagging **`enterprise-wecom-v0.0.7-ga`** at
`0485453`. Non-blocking carry-over (unchanged from prior round): consider
fp32/fp16 bge-m3 ONNX for production embedding quality; `agent-platform` MCP
server availability is an external ops item; PG `ssl=off` is acceptable only
while loopback-only.

## Template render smoke + gateway log-persistence re-verify (production @ 2a3cf00) — PASS

Mac mini rendered a clean standalone project from the `deepagents/enterprise-wecom`
template (`agentseek create deepagents/enterprise-wecom --no-input` →
`~/agentseek-template-smoke/enterprise_wecom_digital_employee/`), copied in the
quasi-production config + assets, and smoke-tested it end-to-end. Confirms the
v0.0.7 GA template produces a deployable standalone project, and that
`run_gateway.sh`'s baked-in log persistence works on the rendered project.

**Render + config.** Template rendered cleanly (project root, scripts/src/vendor/
.agents/.agentseek/launchd/runtime structure). Copied in: `.env` (pgvector +
scram `agentseek_app` URLs + bge-m3 paths), `.agents/mcp.local.json` (5 live MCP
servers), `vendor/dameng/DmJdbcDriver18-8.1.3.62.jar`, `models/bge-m3-onnx/
{model.onnx 570 MB, tokenizer.json}`. `uv sync` (deps from local editable
agentseek packages + `bub-mcp` via git, via the 7890 proxy) → `.venv` 816 MB,
exit 0. `prod_check.py --env-file .env` → **all OK, "Production preflight
passed"** (pgvector backend, dims 1024, bge-m3 model+tokenizer paths exist,
short-term + explicit-long-term use SQLAlchemy URL).

**run_gateway.sh log persistence (rendered project):**
- Default path: `bash scripts/run_gateway.sh` (no outer `>>`) → boot auto-appended
  to `~/Library/Logs/agentseek-wecom/gateway.log` (44→53 lines). fd 1w/2w → that
  file. ✅
- Custom path: `AGENTSEEK_GATEWAY_LOG=/tmp/agentseek-template-gateway.log bash
  scripts/run_gateway.sh` → boot written there; default untouched. ✅
- Append mode (history preserved across restarts). The rendered `run_gateway.sh`
  is identical to the in-repo one except `PROJECT_ROOT` (1 level) vs `REPO_ROOT`
  (3 levels) — expected for a standalone project.

**Live WeCom** (rendered gateway on the custom log path, pid 32432, :12000;
the in-repo gateway was stopped to free the port):

- **A identity** — `我是谁` (朱春霖) → full identity; rendered gateway's full
  chain works (WeCom → DM sidecar pid 39362 → scram `agentseek_app` PG → model).
  ✅
- **D pgvector semantic** — `请长期记住：…负责数据架构工作` → `我的工作职责是什么？`
  recalled "**负责数据架构工作**" via pgvector on the rendered project; new rows
  in `contextseek_pgvector_items`. ✅
- **F MCP** — `列一下当前可用的 MCP 工具` → 4/5 services + risk labels (tavily
  ⚠️ 需确认); `agent-platform` ❌ external SSE down (not a regression). ✅
- B (short-term) / C (explicit long-term) / E (isolation): share A's scram PG
  connection + the same v0.0.7 code + the same shared `agentseek` DB, all
  verified on prior rounds — not re-driven on the rendered project.

**Extra checks (rendered project):** 0 SIGBUS / 0 traceback; rendered
`runtime/contextseek` does not exist (pgvector backend, no seekdb); `mcp-audit`
empty (no MCP tool *called*, only listed → no audit, no guard reason); msgid
dedup fired 2× (normal WeCom retries). `git diff 68d7b25 -- mcp_policy.py
tools.py` → **zero diff** (MCP revert intact).

**Verdict.** The v0.0.7 GA template renders a working standalone project:
preflight passes, gateway boots, log persistence (default + custom) works, and
live identity + pgvector semantic + MCP all function on the rendered project.
**Template sync + log-persistence change verified.**

**State note.** The smoke rendered gateway (pid 32432, custom log path
`/tmp/agentseek-template-gateway.log`) is currently on :12000; the in-repo
gateway was stopped for the smoke. Switch back to the in-repo gateway when done
with the smoke (the rendered `~/agentseek-template-smoke/` project is throwaway).

## v0.0.8 observability foundation (Codex local development) — READY FOR MAC MINI VERIFY

Branch: `enterprise/v0.0.8-observability`.

Scope:
- Added `agentseek_enterprise.observability`, a best-effort enterprise event
  writer. It writes redacted JSONL events to
  `AGENTSEEK_ENTERPRISE_EVENTS_LOG_PATH` when
  `AGENTSEEK_ENTERPRISE_EVENTS_ENABLED=true`; employee/session/scope/namespace
  values are hashed and common secret fields are redacted.
- Added optional Langfuse export behind `AGENTSEEK_LANGFUSE_ENABLED=false` by
  default. Missing Langfuse SDK or missing keys must not break local JSONL events
  or WeCom serving.
- Instrumented the non-MCP business runtime paths: WeCom stream/message/dedup,
  employee identity lookup, short-term memory load/save, explicit durable memory
  recall/write/forget, and pgvector add/retrieve. MCP confirmation behavior is
  intentionally untouched; MCP decisions remain observable through the existing
  `runtime/mcp-audit.jsonl`.
- Added `scripts/admin_events_summary.py` to the example and template for
  local summary of recent enterprise events.
- Updated example/template `.env.example`, `prod_check.py`, README, and template
  reference docs.

Guardrails:
- `git diff 68d7b25 -- contrib/agentseek-enterprise/src/agentseek_enterprise/mcp_policy.py examples/enterprise_wecom_digital_employee/src/enterprise_wecom_digital_employee/tools.py`
  is expected to stay empty. This preserves the MCP confirmation rollback state.
- Observability must be best-effort only: event write or Langfuse failures should
  never fail an employee turn.

Local verification:
- `PYTHONPATH=contrib/agentseek-enterprise/src uv run pytest contrib/agentseek-enterprise/tests -q`
  → 65 passed.
- `uv run pytest contrib/agentseek-wecom/tests -q` → 15 passed, 1 Starlette
  deprecation warning.
- `uv run pytest contrib/agentseek-contextseek/tests -q` → 43 passed, 1 skipped
  (real pgvector integration requires URL).
- `uv run ruff check --no-fix` on touched enterprise/contextseek/wecom/example
  Python files → all checks passed.
- Raw template `tools.py` contains Jinja imports and cannot be parsed directly by
  ruff; the added template `admin_events_summary.py` and `prod_check.py` pass
  isolated ruff.

Mac mini verification requested:
1. Pull `enterprise/v0.0.8-observability`.
2. Add/confirm these env keys in the example `.env`:
   - `AGENTSEEK_ENTERPRISE_EVENTS_ENABLED=true`
   - `AGENTSEEK_ENTERPRISE_EVENTS_LOG_PATH=./runtime/enterprise-events.jsonl`
   - `AGENTSEEK_ENTERPRISE_EVENTS_HASH_SECRET=` (empty is OK; namespace secret is
     used)
   - `AGENTSEEK_LANGFUSE_ENABLED=false` for the first smoke.
3. Run `scripts/prod_check.py --env-file .env` and verify the enterprise event
   log parent is writable.
4. Start the gateway, run live smoke: identity, short-term memory, explicit
   durable memory, pgvector semantic recall, WeCom msgid dedup, and MCP list.
5. Confirm `runtime/enterprise-events.jsonl` receives redacted events and does
   not contain plaintext OA accounts, session ids, tokens, passwords, or
   response URLs.
6. Run `scripts/admin_events_summary.py --path runtime/enterprise-events.jsonl
   --since-hours 24`.
7. Confirm `mcp_policy.py` and example `tools.py` remain zero-diff vs `68d7b25`.

## v0.0.8 observability path fix (Codex local development) — READY FOR MAC MINI VERIFY

Follow-up after Mac mini PASS report at `c8dcd5c`: the event log path worked
when `.env` used an explicit example-relative value, but the default relative
path in `EnterpriseObservabilitySettings.from_env()` was resolved against the
current working directory. That made example runs launched from the repository
root write to the repository root `runtime/` instead of the example project
`runtime/`.

Fix:
- `EnterpriseObservabilitySettings.from_env(project_root=...)` now accepts an
  explicit project root.
- Without an explicit root, relative event paths are resolved against
  `AGENTSEEK_ENTERPRISE_PROJECT_ROOT` when set, then against the parent
  directory of `AGENTSEEK_ENV_FILE`, and only then against CWD.
- `EnterpriseEventWriter(..., project_root=...)` exposes the same rule for
  explicit construction.
- `prod_check.py` in the example and template now checks observability paths
  relative to the env file directory, matching runtime behavior.

Local verification:
- `PYTHONPATH=contrib/agentseek-enterprise/src uv run pytest
  contrib/agentseek-enterprise/tests/test_observability.py -q` → 6 passed.
- `uv run ruff check --no-fix` on touched observability/prod_check files → all
  checks passed.

Mac mini verification requested:
1. Pull the updated `enterprise/v0.0.8-observability` commit.
2. Use `AGENTSEEK_ENV_FILE=examples/enterprise_wecom_digital_employee/.env` and
   set `AGENTSEEK_ENTERPRISE_EVENTS_LOG_PATH=./runtime/enterprise-events.jsonl`.
3. Launch from the repository root through
   `examples/enterprise_wecom_digital_employee/scripts/run_gateway.sh`.
4. Confirm events are written to
   `examples/enterprise_wecom_digital_employee/runtime/enterprise-events.jsonl`,
   not the repository root `runtime/enterprise-events.jsonl`.
5. Run `prod_check.py --env-file examples/enterprise_wecom_digital_employee/.env`
   and confirm it checks the same directory.

## v0.0.8 observability Mac mini verification (2026-07-08) — PASS

Mac mini verified `enterprise/v0.0.8-observability` @ `18ca64e` (new
`agentseek_enterprise.observability` structured event log + event
instrumentation on WeCom/identity/short-term/durable/pgvector paths + optional
Langfuse (off this round) + `admin_events_summary.py`). Not GA — RC/GA decision
after this verification.

**Static — all PASS.** `git diff 68d7b25 -- mcp_policy.py tools.py` → **zero
diff** (MCP confirmation revert intact; v0.0.8 does not touch MCP).
`pytest contrib/agentseek-enterprise/tests -q` → **65 passed** (+observability
tests). `pytest contrib/agentseek-wecom/tests -q` → **15 passed, 1 warning**
(StarletteDeprecationWarning, allowed). `pytest contrib/agentseek-contextseek
/tests -q` → **43 passed, 1 skipped**. Touched `ruff` (observability.py,
plugin.py, long_term_memory.py, test_observability.py, pgvector.py, channel.py,
admin_events_summary.py, prod_check.py) → **All checks passed**.

**Config.** Live `.env` extended with `AGENTSEEK_ENTERPRISE_EVENTS_ENABLED=true`,
`..._LOG_PATH=./runtime/enterprise-events.jsonl`, `..._HASH_SECRET=` (empty →
falls back to namespace secret), `AGENTSEEK_LANGFUSE_ENABLED=false` (Langfuse
not exercised this round). `.env` stays gitignored.

**prod_check — PASS.** `enterprise event log parent is writable` ✅;
`Langfuse tracing disabled` ✅; pgvector/bge-m3/DM-sidecar/PostgreSQL/MCP
checks still green. Gateway restarted on `18ca64e` (pid 1852, clean boot,
:12000, 0 SIGBUS).

**Live WeCom (朱春霖):**

- **A identity** (`我是谁`) → full identity; `identity_lookup` event with
  `cache=miss`, `source=dm`, `status=found`, hashed `employee_key`/`user_key`.
- **C explicit long-term** (`请长期记住：…偏好简洁分点` → `你记得我的回复偏好吗？`)
  → recalled; `durable_memory_write` (status=recorded, category=preference,
  slot=reply_style) + `durable_memory_recall` (status=succeeded, entry_count).
- **F MCP** (`列一下当前可用的 MCP 工具`) → 5 services, risk labels correct.
- B (short-term) / D (pgvector) / E (msgid dedup): their event paths
  (`short_term_memory_load/save`, `contextseek_pgvector_add/retrieve`) **fired
  on the A turn** (save/load + pgvector run per turn) — instrumentation
  confirmed live; functional recall of those subsystems is unchanged from the
  verified v0.0.7 GA. `wecom_duplicate_msgid` did not fire (no natural WeCom
  retry during the smoke); its instrumentation is covered by the wecom unit
  tests.

**`enterprise-events.jsonl` — generated, structured, redacted.** Event types
captured (42 events across the smoke): `wecom_message_received`,
`wecom_stream_started`, `wecom_stream_finished`, `identity_lookup`,
`short_term_memory_load`, `short_term_memory_save`, `durable_memory_write`,
`durable_memory_recall`, `contextseek_pgvector_add`,
`contextseek_pgvector_retrieve` — each with `ts`, `status`, `duration_ms`, and
hashed `*_key`/`principal`/`namespace`/`scope` identifiers (e.g.
`employee_key: hmac-6564…`, `user_key: hmac-4644…`).

**Finding (path resolution) — observability resolves via CWD, not PROJECT_ROOT.**
The events log initially landed at **`runtime/enterprise-events.jsonl` (repo
root)** because `observability.py` resolves `AGENTSEEK_ENTERPRISE_EVENTS_LOG_PATH`
via `path.expanduser()` against the process CWD
([observability.py:119](../../contrib/agentseek-enterprise/src/agentseek_enterprise/observability.py#L119)),
and `run_gateway.sh` does `cd "$REPO_ROOT"`. The MCP audit log instead resolves
via `MCPPolicySettings.from_env(project_root=…)` + `_resolve_path(…, project_root)`
([mcp_policy.py:55-79](../../contrib/agentseek-enterprise/src/agentseek_enterprise/mcp_policy.py#L55)),
landing in the **example** `runtime/mcp-audit.jsonl`. So the two were
inconsistent (events at repo root, audit at example root) — the events log
should live under the example/template project like the audit log.

**Workaround applied on Mac mini (config layer, verified):** set
`AGENTSEEK_ENTERPRISE_EVENTS_LOG_PATH=examples/enterprise_wecom_digital_employee/runtime/enterprise-events.jsonl`
in `.env`; migrated the 34 already-captured events to that path; restarted the
gateway. Confirmed new events now land in the example `runtime/` (8 events from
the post-restart identity turn, all at the example path) and the repo-root path
receives nothing. The running gateway now writes events alongside the MCP audit
in the example project's `runtime/`.

**Code fix recommended for Codex (proper default):** `observability.from_env`
should accept a `project_root` parameter and resolve `EVENTS_LOG_PATH` relative
to it (mirroring `mcp_policy.MCPPolicySettings.from_env`), so the default
`./runtime/enterprise-events.jsonl` lands in the example/template `runtime/`
without a per-deployment `.env` override. Non-blocking for v0.0.8-rc1 (the
config workaround holds), but should be fixed before GA so the template default
is correct out of the box.

**Redaction — PASS.** `grep zhuchunlin` → 0; `wecom:zhuchunlin` → 0;
`password|access_token|secret|EncodingAESKey` → 0. No plaintext OA account,
session id, or credential in the events file.

**`admin_events_summary.py --since-hours 24` — PASS.** Outputs Total events
(26), Active hashed principals (4), Top events, Statuses (succeeded/n-a/found/
recorded), and Durations with avg/p95 (e.g. `contextseek_pgvector_add avg=85ms
p95=107ms`, `durable_memory_write avg=10ms`). No plaintext principals in the
summary.

**MCP separation — PASS.** `enterprise-events.jsonl` contains **0** `mcp`
entries — MCP decisions still go to `runtime/mcp-audit.jsonl` (now 24 lines,
guard-reason count **unchanged at 8** historical → revert intact; the +7
non-guard audit rows are normal succeeded/confirmation_required MCP calls from
prior rounds). Observability does not duplicate or replace MCP audit.

**Health.** Current v0.0.8 session (pid 1852, from 14:20) → **0 SIGBUS / 0
exit-138 / 0 Traceback / 0 UniqueViolation / 0 data_inspection_failed**. (The
persistent gateway log also shows 4 stale `Traceback` lines from earlier
pre-v0.0.8 sessions — the persistent log accumulates across restarts; none are
from this v0.0.8 session.)

**Verdict — PASS, recommend v0.0.8-rc1.** Enterprise structured event logging
works end-to-end: all instrumented paths emit hashed/redacted events, the JSONL
is written reliably, event-write failure is isolated from business turns
(verified — turns succeeded throughout), `admin_events_summary.py` works, MCP
revert + audit separation intact, 0 SIGBUS in session. Langfuse export was not
exercised (`LANGFUSE_ENABLED=false`) — that's the next round when a Langfuse
host/keys are available. **One finding for Codex:** the events-log path
resolution uses CWD instead of PROJECT_ROOT (inconsistent with MCP audit); a
config workaround is applied + verified on Mac mini, but Codex should fix
`observability.from_env` to take `project_root` before GA so the template
default lands in the project `runtime/` (see the path-resolution finding above).

### v0.0.8 path-resolution fix re-verified (7504153) — PASS, finding resolved

Codex fixed the path-resolution finding in `7504153`:
`EnterpriseObservabilitySettings.from_env(project_root=…)` +
`EnterpriseEventWriter(…, project_root=…)`, default resolution order
absolute → `project_root` → `AGENTSEEK_ENTERPRISE_PROJECT_ROOT` →
`AGENTSEEK_ENV_FILE` dir → CWD. Mac mini re-tested with the **config workaround
removed** (no `AGENTSEEK_ENTERPRISE_EVENTS_LOG_PATH` in `.env`, so the code's
default `./runtime/enterprise-events.jsonl` is exercised) and the gateway
launched from repo root via `run_gateway.sh`
(`AGENTSEEK_ENV_FILE=examples/…/.env`).

Result — with no `.env` override, events **default-resolve to
`examples/enterprise_wecom_digital_employee/runtime/enterprise-events.jsonl`**
(8 events from one identity turn: `wecom_message_received`,
`wecom_stream_started/finished`, `identity_lookup`, `short_term_memory_load/save`,
`contextseek_pgvector_add/retrieve`), and the **repo-root
`runtime/enterprise-events.jsonl` is not created**. The finding is resolved —
the default now lands in the example/template project `runtime/`, consistent
with the MCP audit log, no per-deployment `.env` override needed. `git diff
68d7b25 -- mcp_policy.py tools.py` still zero; enterprise tests **68 passed**.
The earlier `.env` `EVENTS_LOG_PATH` workaround was removed and is no longer
required.
