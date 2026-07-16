# Enterprise WeCom Digital Employee

Enterprise WeCom digital employee scaffolded from `deepagents/enterprise-wecom`.

It runs a DeepAgents agent through AgentSeek gateway, receives WeCom intelligent robot callbacks, resolves the WeCom user to an employee context, and exposes business MCP tools from `.agents/mcp.json`.

The template injects `state["employee_context"]` and `state["short_term_memory"]` into the model-visible message list, so questions like `我是谁` and follow-ups like `我刚才说我要去哪里` can be answered from runtime context instead of asking the user to restate their OA account or prior message. It also configures a tenant-and-employee scoped persistent `StoreBackend` for explicitly requested durable preferences and work context, plus ContextSeek semantic recall across the same employee's sessions.

## Deployment Baseline

Use the repository `production` branch for internal deployment and trial use.
It points at the current Enterprise WeCom GA baseline. Use the GA tag when you
need an immutable rollback or audit target.

```bash
git clone -b production http://172.200.6.12:9091/zhuchunlin/agentseek-enterprise.git
```

To pin the verified v0.0.9 build:

```bash
git clone --branch enterprise-wecom-v0.0.9-ga http://172.200.6.12:9091/zhuchunlin/agentseek-enterprise.git
```

GitHub mirrors both refs at `https://github.com/sambazhu/agentseek-enterprise.git`.

### v0.0.9 GA

`enterprise-wecom-v0.0.9-ga` adds verified AI Bot file intake, MinerU OCR,
CurrentFiles refresh, complete large-file analysis, multi-sheet XLSX statistics,
and ordered PPTX extraction. The final Mac mini audit passed every gate in
`GA_READINESS_V0.0.9.md`.

Keep `enterprise-wecom-v0.0.8-ga` as the immutable rollback tag.

Use `RC1_RUNBOOK.md` and `GA_READINESS_V0.0.9.md` as the acceptance record.

## Setup

```bash
uv sync
cp .env.example .env
```

Fill `.env` with:

- model provider credentials;
- WeCom callback `Token` and `EncodingAESKey`;
- self-built WeCom app `corp_id` and app secret;
- employee identity database settings;
- macOS DM access uses the committed JDBC driver plus Java 11. Keep
  `AGENTSEEK_IDENTITY_DM_EXECUTION_MODE=subprocess` as the conservative rollback
  mode; use `AGENTSEEK_IDENTITY_DM_EXECUTION_MODE=sidecar` after validating the
  long-lived local worker in the target network environment. Both modes keep
  JPype/libjvm out of the gateway process so ContextSeek `pgvector`/ONNX can
  run in the main process. See `DEPLOYMENT_NOTES.md` for the FlClash/TUN route
  workaround;
- short-TTL employee identity cache settings, so repeated messages from the
  same resolved employee do not reopen the DM/JDBC path on every turn;
- short-term memory retention settings and, for production, an optional
  `AGENTSEEK_ENTERPRISE_MEMORY_SQLALCHEMY_URL` pointing at PostgreSQL/MySQL.
  Prompt injection is independently bounded by
  `AGENTSEEK_ENTERPRISE_MEMORY_PROMPT_MAX_CHARS` and
  `AGENTSEEK_ENTERPRISE_MEMORY_PROMPT_MAX_MESSAGE_CHARS`;
- the tenant id, namespace secret, and durable store path, or
  `AGENTSEEK_ENTERPRISE_STORE_SQLALCHEMY_URL` for PostgreSQL/MySQL;
- ContextSeek semantic storage: PostgreSQL + `pgvector` with bge-m3 dense
  embeddings exported to ONNX. Local `seekdb` remains available for development
  or rollback only;
- inbound file settings. `agentseek-wecom` can receive WeCom `file`, `image`,
  `video`, and `mixed` callbacks. AI Bot file/image/video downloads use the
  callback JSON's short-lived signed URL and decrypt the downloaded bytes with
  the callback `EncodingAESKey`. `voice.content` is already transcribed text and
  is handled as text input. `agentseek-files` stores attachments under
  HMAC-scoped tenant/employee/session directories and exposes only extracted
  text or safe metadata to the model;
- MinerU settings when `AGENTSEEK_FILES_EXTRACTOR=mineru`. Text-like files
  (`.txt`, `.md`, `.csv`, `.json`) are extracted locally. PDF, Office, and image
  files are submitted to MinerU when configured. If extraction is still pending,
  the WeCom stream says the file is being parsed; the runtime polls for up to
  300 seconds and refreshes completed file context on the employee's next turn;
- MCP servers in `.agents/mcp.json`;
- MCP policy and audit settings for allowlists, write-tool confirmation, and
  JSONL audit logs.

When you enable PostgreSQL/MySQL memory URLs, install the matching SQLAlchemy
driver in the deployment environment, for example `psycopg[binary]` for
`postgresql+psycopg://...` or `pymysql` for `mysql+pymysql://...`.

When you enable `AGENTSEEK_CTX_STORAGE_BACKEND=pgvector`, install PostgreSQL
with the `vector` extension and make `psycopg[binary]` available in the project
environment. The gateway creates the semantic table on first startup. Point
`AGENTSEEK_CTX_BGE_M3_ONNX_MODEL_PATH` plus
`AGENTSEEK_CTX_BGE_M3_TOKENIZER_PATH` at an exported bge-m3 ONNX model
directory. This path uses onnxruntime and `tokenizers`, not torch.

### Strategic Development Department Knowledge

M2-02 adds a local read-only MCP simulator for the industry-report digital
employee. It uses PostgreSQL `pgvector` for semantic retrieval and `pg_trgm`
for keyword retrieval. It is separate from employee ContextSeek memory.

Install both extensions in the target database, then set the
`AGENTSEEK_DEPARTMENT_KNOWLEDGE_*` variables from `.env.example`. The simulator
reuses the ContextSeek bge-m3 model paths when its dedicated model paths are
empty.

Merge the `department-knowledge` entry from
`.agents/mcp.department-knowledge.example.json` into the deployment's existing
`.agents/mcp.json` or `mcp.local.json`. Keep the existing Gildata and Tavily
servers; do not replace the whole file.

Place approved Word samples in an administrator-controlled directory outside
the WeCom upload runtime, then import them:

```bash
uv run --env-file examples/enterprise_wecom_digital_employee/.env \
  python examples/enterprise_wecom_digital_employee/scripts/import_department_knowledge.py \
  /path/to/approved-strategy-documents
```

Supported import formats are `.docx`, `.md`, and `.txt`. Import is deterministic:
re-importing a source filename in the same collection updates its chunks. Employee
uploads remain request-scoped files and are never promoted automatically.

The MCP contract exposes `knowledge_list_documents`, `knowledge_search`, and
`knowledge_read_chunks`. For report work, query this department source first.
If evidence remains missing, explain the gap and ask the employee before using
external MCP tools.

M2-03 adds a template-driven formal path. Create a WorkItem, save a lightweight
ReportBrief, show its exact version to the requester, and confirm it only after an
explicit reply. The confirmation tool enforces that requirement server-side against
the latest human message and exact version; prompt wording alone cannot promote a
provisional brief. `run_internal_report_research` then executes the Pack-pinned
section questions against `department-knowledge`, stores provenance-only
SourceRecords in the work ledger, and returns section coverage and unresolved
gaps. It does not write report prose or call Tavily/Gildata.

M2-04 closes the knowledge-gap decision loop. `get_report_research_gaps`
rebuilds current internal coverage from immutable SourceRecords and returns four
version-bound choices: Gildata, Tavily public search, upload request-scoped
materials, or continue with visible gaps. `resolve_report_research_gaps` accepts
only an unambiguous choice in the latest employee message for the exact
ReportBrief version. The choice is stored as a confirmed, versioned
`report-research-gap-decision` WorkContract. Gildata/Tavily results are registered
as provenance-only SourceRecords without storing the raw provider response;
Evidence, Claims, and report prose remain M3/M4 work.

Pack `1.2.0` makes the report topic itself a first-class evidence question. Its
`report_topic` query strategy searches the exact ReportBrief title before the
five reusable industry questions, so generic securities-methodology documents
cannot silently count as direct support for a named institution, policy, event,
period, or figure. The M2 delivery boundary also applies a deterministic output
guard after the agent run: bare confirmation and attempted report prose are
replaced with a fail-closed operational reply and a redacted
`report_output_guard` event. Coverage tables and section labels remain valid
operational output; the event records only an output digest, shape signals, and
top-level tool sequence, never the raw employee or model text. `choices=[]`
never advertises report generation.
See `DEPARTMENT_KNOWLEDGE_MCP.md` for the replacement contract.

When running this example from the AgentSeek repository root, keep the root
`.env` small and point it at this project's dotenv file:

```bash
AGENTSEEK_ENV_FILE=examples/enterprise_wecom_digital_employee/.env
```

The AgentSeek CLI loads that file before plugins are initialized, so the WeCom,
enterprise identity, schedule, LangChain, and ContextSeek plugins all see the
same project-scoped configuration.

## Run

```bash
examples/enterprise_wecom_digital_employee/scripts/run_gateway.sh
```

This example also declares `.agentseek/lifecycle.toml`, so the new AgentSeek
lifecycle commands can inspect and start it from the example directory:

```bash
agentseek info
agentseek doctor
agentseek dev
agentseek task prod-check
```

Before installing launchd, run the production preflight. It redacts secrets and
checks only presence, file paths, writable runtime directories, tracing intent,
identity isolation mode, and namespace-secret readiness:

```bash
examples/enterprise_wecom_digital_employee/scripts/prod_check.py --env-file examples/enterprise_wecom_digital_employee/.env
```

Generate a new namespace secret before formal production handoff:

```bash
examples/enterprise_wecom_digital_employee/scripts/prod_check.py --generate-namespace-secret
```

The GA baseline is frozen in `PRODUCTION_FREEZE.md`. Use it to pin the verified
commit/tag, GitHub release, company GitLab mirror, required runtime switches,
smoke-test prompts, and rollback knobs before changing production behavior.

For production deployment, prefer the immutable GA tag:

```bash
git checkout enterprise-wecom-v0.0.9-ga
```

For Mac mini process supervision, edit the repo path in
`launchd/com.local.agentseek-enterprise-wecom.plist`, then install it as a
user LaunchAgent:

```bash
mkdir -p ~/Library/LaunchAgents
cp examples/enterprise_wecom_digital_employee/launchd/com.local.agentseek-enterprise-wecom.plist ~/Library/LaunchAgents/
launchctl unload ~/Library/LaunchAgents/com.local.agentseek-enterprise-wecom.plist 2>/dev/null || true
launchctl load -w ~/Library/LaunchAgents/com.local.agentseek-enterprise-wecom.plist
```

The WeCom callback listens on port `12000` and path:

```text
/ai-bot/callback/demo/<botid>
```

## Smoke Test

After configuring WeCom, send `你好` to the intelligent robot. A healthy first response should show that:

- `userid` is converted from encrypted robot `open_userid` to plaintext WeCom userid;
- `oa_account` matches that plaintext userid;
- `employee_context` is present in runtime state.
- follow-up questions can use recent messages stored under `short_term_memory`.
- semantic retrieval is scoped to the resolved employee rather than the WeCom session.

Use these smoke tests to keep the memory layers distinct:

### A. Identity

```text
我是谁
```

Expected: the answer uses `employee_context`, including name, OA account,
organization path, role, and post when available.

### B. Short-Term Memory (Relational Store)

```text
帮我记一下，我明天下午去深圳出差
我刚才说我要去哪里？
```

Expected: the answer recalls the recent Shenzhen trip from the short-term
conversation memory database. Production deployments can set
`AGENTSEEK_ENTERPRISE_MEMORY_SQLALCHEMY_URL` to PostgreSQL/MySQL; local
development falls back to `AGENTSEEK_ENTERPRISE_MEMORY_SQLITE_PATH`. The
newest messages are retained first when the configured prompt character budget
is reached, preventing large research/tool replies from bloating later turns.

### C. Explicit Durable Memory (Employee Store)

```text
请长期记住：我偏好简洁、分点的回复方式
你记得我的回复偏好吗？
```

Expected: the answer recalls the preference through the dedicated durable memory
tools. Production deployments can set
`AGENTSEEK_ENTERPRISE_STORE_SQLALCHEMY_URL` to PostgreSQL/MySQL; local
development falls back to `AGENTSEEK_ENTERPRISE_STORE_SQLITE_PATH`. This is
explicit employee memory, not ContextSeek semantic recall.
Distinct work duties coexist under scoped responsibility slots (for example,
`responsibility.data_arch` and `responsibility.ai_arch`). The runtime never
uses the forget tool for deduplication: deletion requires explicit wording in
the employee's latest message and an exact stored-memory match.

To compact legacy duplicates and slot aliases in the authenticated employee's
profile, ask `请清理重复记忆`. Ask `请清理过期记忆和历史行程` to additionally
remove travel/meeting entries with past absolute dates. Both operations require
explicit wording in the latest employee message.

### D. Semantic Long-Term Memory (ContextSeek + pgvector)

```text
请长期记住：我的职责是负责数据架构工作
我的工作职责是什么？
```

Expected: ContextSeek retrieves the semantically related historical turn and the
model answer includes the data-architecture responsibility. In production this
comes from `AGENTSEEK_CTX_PGVECTOR_TABLE` using bge-m3 dense vectors. With
`AGENTSEEK_CTX_STORAGE_BACKEND=seekdb`, the same smoke test can be used for
local fallback validation. This is semantic memory, not the explicit durable
memory tool.

### E. MCP

After adding servers to `.agents/mcp.json`, restart the gateway, then ask:

```text
列一下当前可用的 MCP 工具
```

Expected: the answer lists the configured MCP services and tools.

### F. Inbound Files

Send a small `.txt` file from WeCom, then ask:

```text
总结一下我刚才发的文件
```

Expected: the file is stored below `runtime/files/<tenant>/<employee>/<date>/<session>/inbound/`,
the path uses HMAC keys rather than plaintext OA accounts, and the answer uses
the `[CurrentFiles]` context injected by `agentseek-files`.

For PDF, Office, or image files, set:

```env
AGENTSEEK_FILES_ENABLED=true
AGENTSEEK_FILES_EXTRACTOR=mineru
AGENTSEEK_FILES_MIXED_PDF_BG_OCR=true
AGENTSEEK_MINERU_BASE_URL=https://mineru.net
AGENTSEEK_MINERU_TOKEN=<your-token-for-the-v4-extract-api>
AGENTSEEK_MINERU_MODEL_VERSION=vlm
AGENTSEEK_MINERU_OCR_MODEL_VERSION=pipeline
AGENTSEEK_MINERU_IS_OCR=false
AGENTSEEK_MINERU_POLL_TIMEOUT_S=300
```

Expected: the file is accepted and submitted to MinerU when the extension is in
`AGENTSEEK_FILES_ALLOWED_EXTENSIONS`. With a token, local files use MinerU's v4
upload API. The default `AGENTSEEK_MINERU_IS_OCR=false` means auto-detect: first
run the configured `vlm` model without OCR, then retry an image-only scanned PDF
once with `pipeline` plus OCR. Set it to `true` only to force OCR immediately.
When `AGENTSEEK_FILES_MIXED_PDF_BG_OCR=true`, a PDF whose first result contains
both substantive text and image placeholders is returned immediately, while a
second full OCR pass runs in the background. A completed background result
replaces the first extraction for the employee's next turn; failure preserves
the usable first-pass text.
Without a token, the lightweight Agent API remains available as a compatibility
fallback. Pending file records are refreshed from disk or MinerU on the
employee's next turn, so completed text replaces the earlier pending snapshot.
Background OCR lifecycle fields (`mixed_pdf_bg_ocr`, `bg_ocr_status`, and
`bg_ocr_task_id`) are persisted at the top level of each file's `metadata.json`.

v0.1.0 follow-up: MinerU can leave signatures, seals, logos, flowcharts, and
architecture diagrams as image references. An optional VLM image-description
stage can replace those references with model-generated visual summaries; it is
not part of the v0.0.9 receive pipeline.

### MCP Policy And Audit

The template's `call_mcp_tool` adapter enforces a local enterprise policy before
it calls a configured MCP server. Think of it as a small gate in front of every
business tool call: classify the tool, decide whether it can run, require
confirmation when needed, and write an audit event.

By default the policy keeps existing query tools available and writes audit
events to `./runtime/mcp-audit.jsonl`.

Risk levels:

| Risk | Use for | Default behavior |
| --- | --- | --- |
| `read` | Data lookup, search, report retrieval | Allowed unless denylist/default-deny blocks it |
| `write` | Meeting-room booking, travel submission, workflow creation | Requires explicit confirmation |
| `risky` | Cancellation, permission changes, skill installation, high-impact actions | Requires explicit confirmation |

Policy actions:

| Action | Meaning |
| --- | --- |
| `allow` | The adapter calls the remote MCP tool. |
| `deny` | The adapter returns a policy-denied message and does not call MCP. |
| `confirm` | The adapter returns a confirmation-required message and does not call MCP. |

Use `server/tool` patterns to classify business tools:

```env
AGENTSEEK_ENTERPRISE_MCP_ALLOWLIST=gildata_datamap-*/*,tavily-search/*
AGENTSEEK_ENTERPRISE_MCP_WRITE_TOOLS=office/book_room,oa/submit_travel
AGENTSEEK_ENTERPRISE_MCP_RISKY_TOOLS=oa/cancel_request
AGENTSEEK_ENTERPRISE_MCP_CONFIRM_TOOLS=
AGENTSEEK_ENTERPRISE_MCP_REQUIRE_CONFIRMATION=true
AGENTSEEK_ENTERPRISE_MCP_AUDIT_LOG_PATH=./runtime/mcp-audit.jsonl
```

Patterns also accept `server:tool` and wildcards such as
`gildata_datamap-*/*`.

For early rollout, keep `AGENTSEEK_ENTERPRISE_MCP_DEFAULT_ACTION=allow` and
classify only known write/risky tools. After the tool inventory is stable,
switch to a stricter allowlist:

```env
AGENTSEEK_ENTERPRISE_MCP_DEFAULT_ACTION=deny
AGENTSEEK_ENTERPRISE_MCP_ALLOWLIST=gildata_datamap-*/*,tavily-search/tavily_search,office/*,oa/*
AGENTSEEK_ENTERPRISE_MCP_WRITE_TOOLS=office/book_room,oa/submit_travel
AGENTSEEK_ENTERPRISE_MCP_RISKY_TOOLS=oa/cancel_request,agent-platform/install_agent_skills
AGENTSEEK_ENTERPRISE_MCP_CONFIRM_TOOLS=tavily-search/tavily_search
```

Confirmation flow:

1. The model calls `call_mcp_tool(..., confirmed=false)`.
2. The adapter returns a confirmation-required response for `write`, `risky`, or
   explicitly confirmed tools.
3. The model summarizes the exact business action and key arguments to the
   employee.
4. After the employee clearly confirms, the model calls the same tool again with
   `confirmed=true`.
5. Only the second call reaches the remote MCP server.

Audit events are JSONL records with `timestamp`, `tool_ref`, `action`, `risk`,
`confirmed`, policy `reason`, redacted `arguments`, and a truncated
`result_summary` or error. Argument keys containing password, secret, token, api
key, private key, credential, `身份证`, `银行卡`, `密码`, `密钥`, or `令牌` are
written as `[REDACTED]`.

Slow tools still need response budgeting. A confirmed MCP call can succeed while
the final WeCom reply times out if the model spends too long processing a large
tool result. Prefer bounded tool output, answer-first summaries, or an async
"正在处理" workflow for slow external tools.

Distinct messages for the same WeCom session are processed by one bounded,
arrival-ordered worker. Other employee sessions remain concurrent. Tune the
model, stream, queue, and graceful-shutdown limits with:

```env
AGENTSEEK_LANGCHAIN_RUN_TIMEOUT_SECONDS=180
AGENTSEEK_WECOM_TURN_TIMEOUT_SECONDS=195
AGENTSEEK_WECOM_SESSION_QUEUE_MAXSIZE=3
AGENTSEEK_WECOM_QUEUE_WAIT_TIMEOUT_SECONDS=240
AGENTSEEK_WECOM_SHUTDOWN_TIMEOUT_SECONDS=10
```

Ordinary AI Bot turns use one delivery path even when the callback contains
`response_url`: the callback returns an unfinished `stream` with the acknowledgement
or queue position, and WeCom polls that same stream until its terminal answer,
rejection, or timeout. Queue status and rejection can finish immediately on the
same stream. The one-shot `response_url` is reserved for explicitly asynchronous
work such as a pending file extraction; it is never mixed into a normal chat turn.

When DM identity runs in long-lived sidecar mode, keep both deadlines enabled:

```bash
AGENTSEEK_IDENTITY_DM_SIDECAR_TIMEOUT_SECONDS=8
AGENTSEEK_ENTERPRISE_IDENTITY_LOOKUP_TIMEOUT_SECONDS=15
```

The inner deadline force-kills and recycles a stuck JDBC/JVM sidecar. The outer deadline runs the
blocking provider outside the event loop and degrades the current turn to identity
status `error`, so one failed lookup cannot freeze other sessions or shutdown.

Keep the WeCom timeout slightly above the LangChain timeout. A timed-out model
turn is cancelled and receives a terminal stream response so the next queued
message can continue. The queue limit counts pending messages, not the active
turn: the default permits one active turn plus three waiting messages. Each
accepted waiting stream immediately displays its queue position; further
messages are rejected before they reach the agent. A pending message that does
not start within the queue-wait timeout is finished as expired. Employees can
send `查看消息队列` or `查看排队状态` for an immediate status response that does
not enter the model queue. AI Bot callbacks carrying `response_url` receive a
completed initial ACK/queue response; the final answer or queue-timeout notice
uses that one-shot URL. This avoids relying on clients to render intermediate
`finish=false` stream content. Callbacks without `response_url` retain legacy
stream polling.

Redacted `langchain_run_stage` events show time spent resolving the runnable,
enriching prompt state, building invocation context, and entering/completing the
model invocation. They record counts and durations, never prompt or reply text.
`langchain_model_call` emits `started` before provider I/O and then a terminal
status. Configure the inner provider boundary with
`AGENTSEEK_MODEL_REQUEST_TIMEOUT_SECONDS` and `AGENTSEEK_MODEL_MAX_RETRIES`.

### Enterprise Observability

The gateway can emit redacted structured events to
`./runtime/enterprise-events.jsonl` when
`AGENTSEEK_ENTERPRISE_EVENTS_ENABLED=true`. These events are separate from MCP
audit logs:

- enterprise events: runtime health, WeCom streams, identity lookup,
  short-term memory, durable memory, and pgvector semantic memory;
- MCP audit: policy decision records for regulated tool calls.

Employee identifiers, sessions, ContextSeek scopes, and namespaces are hashed
before they are written. Common secret fields such as password, token, secret,
API key, credential, and private key are redacted.

Relative `AGENTSEEK_ENTERPRISE_EVENTS_LOG_PATH` values are resolved against the
project root, inferred from `AGENTSEEK_ENV_FILE` when the gateway is launched
from the repository root.

Quick summary:

```bash
examples/enterprise_wecom_digital_employee/scripts/admin_events_summary.py \
  --path examples/enterprise_wecom_digital_employee/runtime/enterprise-events.jsonl \
  --since-hours 24
```

Optional Langfuse export is controlled by:

```env
AGENTSEEK_LANGFUSE_ENABLED=false
LANGFUSE_PUBLIC_KEY=
LANGFUSE_SECRET_KEY=
LANGFUSE_HOST=
AGENTSEEK_LANGFUSE_ENV=production
AGENTSEEK_LANGFUSE_RELEASE=enterprise-wecom-v0.0.8
AGENTSEEK_LANGFUSE_TRACE_NAME=agentseek.enterprise
AGENTSEEK_LANGFUSE_FLUSH=true
AGENTSEEK_LANGFUSE_SAMPLE_RATE=1.0
```

The example includes the Langfuse Python SDK in `pyproject.toml`. After setting
the keys and host, run a single probe before enabling long-running gateway
traffic:

```bash
uv sync
examples/enterprise_wecom_digital_employee/scripts/probe_langfuse_event.py \
  --env-file examples/enterprise_wecom_digital_employee/.env
```

The probe exits non-zero if Langfuse is disabled, keys are missing, the SDK is
missing, or the SDK call fails. If Langfuse becomes unavailable during gateway
runtime, local JSONL events continue to work and the gateway should keep serving
WeCom requests.

## What's Different Vs. Pure DeepAgents

- `src/enterprise_wecom_digital_employee/agent.py` exports `build_spec()` for `AGENTSEEK_LANGCHAIN_SPEC`.
- `src/enterprise_wecom_digital_employee/tools.py` adds a lightweight MCP list/call adapter.
- `AGENTS.md` and `skills/` carry enterprise identity and office workflow rules.
- The MCP adapter enforces allowlist/denylist policy, write/risky
  confirmation, and redacted JSONL audit logging before calling remote tools.
- Short-term memory and explicit durable employee memory can both use
  PostgreSQL/MySQL through SQLAlchemy URLs. If those URLs are empty, the example
  falls back to the local SQLite files configured by
  `AGENTSEEK_ENTERPRISE_MEMORY_SQLITE_PATH` and
  `AGENTSEEK_ENTERPRISE_STORE_SQLITE_PATH`.
- DeepAgents uses an isolated `CompositeBackend`: only `AGENTS.md` and `skills/` are copied into a read-only virtual filesystem. Durable `/memories` storage is mapped to a tenant-and-employee scoped `StoreBackend`, but only dedicated memory tools can access it. The agent cannot read the project directory, `.env`, or other host paths, and cannot write files or execute local commands.
- ContextSeek only stores final conversation turns, not MCP calls or tool
  output. Retrieved history is marked as untrusted context and injected as a
  system message. PostgreSQL + pgvector is the production semantic backend when
  `AGENTSEEK_CTX_STORAGE_BACKEND=pgvector`; SeekDB remains a local fallback.
  it uses bge-m3 dense embeddings with `vector(1024)` and keeps the same
  enterprise employee scope contract.
- `agentseek-files` handles inbound file storage and extraction. WeCom AI Bot
  media callbacks are downloaded from the signed callback URL, decrypted with
  `EncodingAESKey`, saved under hashed tenant/employee/session paths, and
  injected as `[CurrentFiles]` system context. Host paths, signed media URLs,
  response URLs, MinerU tokens, and raw file bytes are not exposed to the model.
- DM JDBC identity lookup can run in a short-lived subprocess or a persistent
  local sidecar process. Both keep JPype/libjvm out of the gateway process so
  pgvector/ONNX can coexist with the DM driver. `subprocess` is the
  conservative rollback mode; `sidecar` avoids cold-starting the JVM on cache
  misses.
- Successful employee identity lookups can be cached briefly in the gateway
  process. Missing users and lookup errors are not cached.
- The WeCom channel deduplicates intelligent-robot retries by `msgid` and
  reuses the original stream response, so slow first replies do not launch
  duplicate agent turns.
- `pyproject.toml` depends on AgentSeek runtime plugins: `agentseek-langchain`, `agentseek-wecom`, `agentseek-enterprise`, `agentseek-schedule-sqlalchemy`, and `bub-mcp`.
