# Enterprise WeCom Digital Employee — Deployment Notes (Mac mini)

Handoff notes from deploying/verifying this example on a company Mac mini
(branch `enterprise/wecom-runtime`, then integration branch
`enterprise/wecom-runtime-v0.0.4`, 2026-06-27 through 2026-06-29). Covers the
DM-connection root cause, the upstream integration context, the working
configuration, known-issue workarounds, and the production-ready operating
state.

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

- **MCP policy and audit:** the current `call_mcp_tool` adapter is a generic MCP
  bridge. Before adding state-changing office tools such as meeting-room booking
  or travel submission, add runtime policy for tool allowlists, read/write
  classification, explicit confirmation, argument redaction, and audit logging.
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
