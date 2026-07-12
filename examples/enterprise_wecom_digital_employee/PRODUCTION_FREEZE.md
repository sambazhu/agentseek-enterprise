# Enterprise WeCom Production Freeze

This document freezes the current production baseline for the enterprise WeCom
digital employee runtime.

## Baseline

- Recommended deployment branch: `production`
- Final GA tag: `enterprise-wecom-v0.0.9-ga`
- Final GA tag commit: `8128aac4c37a46264477709adf07bd99e5eadb58`
- Documentation branch commit after GA: current `enterprise/v0.0.9-files-plugin`
- Previous GA tag: `enterprise-wecom-v0.0.8-ga`
- Previous GA commit: `5833571`
- Earlier GA tag: `enterprise-wecom-v0.0.6-ga-20260702`
- Earlier GA commit: `7b442a5`
- Earlier GA tag: `enterprise-wecom-v0.0.5-ga-20260630`
- Earlier production freeze tag: `enterprise-wecom-v0.0.4-prod-20260629`
- Verification host: company-network Mac mini
- Verification date: 2026-07-12

The v0.0.9 GA retains the v0.0.8 identity, memory, pgvector, MCP policy/audit,
and Langfuse baseline. It adds WeCom AI Bot file intake, signed-URL download and
AES decryption, scoped file storage, MinerU extraction and OCR, CurrentFiles
refresh, complete large-file analysis, multi-sheet XLSX statistics, and ordered
PPTX extraction.
Runtime deployments should pin the GA tag for exact
reproducibility or use `production` for the latest documented deployment
baseline. The older v0.0.8, v0.0.7, v0.0.6, v0.0.5, and v0.0.4 tags are kept immutable
for audit history.

## v0.0.9 File Intake And Analysis

- WeCom AI Bot `file`, `image`, `video`, `voice`, and mixed callbacks are
  accepted without changing the v0.0.8 text-message path.
- Signed media URLs are downloaded and decrypted with the configured WeCom
  EncodingAESKey before HMAC-scoped storage.
- Text-like files are extracted locally. PDF, DOCX, XLSX, PPTX, and supported
  images use MinerU when configured.
- Digital documents use a non-OCR first pass. Scanned files retry with OCR.
  Mixed documents can improve in the background without blocking the first reply.
- CurrentFiles refreshes completed extraction on later turns. The read-only
  `analyze_file` tool evaluates complete extracted text for large-file queries.
- Multi-sheet XLSX analysis preserves sheet names and aggregates all matching
  tables. PPTX extraction preserves slide order and available OCR text.

The final Mac mini audit passed 189 automated tests, live identity and memory
checks, pgvector recall, MCP inspection, file-path checks, and observability and
redaction gates. Gateway health remained at zero SIGBUS, traceback, and non-200
callback responses during the final audit.

## v0.0.8 Observability

This GA adds a dual observability path:

- local structured JSONL runtime events remain available for offline diagnosis;
- Langfuse can receive sanitized runtime traces when enabled by environment;
- trace names are structured event names, not raw log payloads;
- employee identity, WeCom session ids, and content fields are HMAC-hashed or
  redacted before leaving the gateway;
- MCP audit remains separate in `runtime/mcp-audit.jsonl`.

The Mac mini validation covered probe delivery, live identity, short-term
memory, explicit durable memory, pgvector semantic recall, MCP tool listing,
redaction checks in Langfuse, and frozen-lock installation from scratch.

## v0.0.7 PostgreSQL And pgvector Runtime

This GA moves the production data path to PostgreSQL:

- short-term conversation memory can use
  `AGENTSEEK_ENTERPRISE_MEMORY_SQLALCHEMY_URL`;
- explicit durable employee memory can use
  `AGENTSEEK_ENTERPRISE_STORE_SQLALCHEMY_URL`;
- ContextSeek semantic memory uses `AGENTSEEK_CTX_STORAGE_BACKEND=pgvector`;
- the semantic backend stores employee-scoped bge-m3 dense vectors in
  `contextseek_pgvector_items` by default;
- the verified deployment uses a dedicated non-superuser `agentseek_app` role
  and SCRAM authentication in `pg_hba.conf`.

SeekDB remains available as a local development or rollback backend. It is not
the v0.0.7 production semantic-memory baseline.

## Durable Employee Memory

Durable employee memory keeps the v0.0.6 behavior:

- P0 write-side near-duplicate deduplication: same category, similar text,
  latest wording wins.
- P3 recall-side cleanup: old dirty profiles are rendered as a deduped read-only
  view without migrating stored data.
- P1/P5 slot supersession: new slot-tagged memories use
  `category + slot` as the identity. Same slot + similar value is a silent
  near-duplicate update; same slot + different value supersedes the old value
  and returns an explicit old-to-new notice for the employee.
- Profile write serialization: `remember_employee_memory` and
  `forget_employee_memory` serialize the full `get -> modify -> put` section per
  employee namespace, preventing same-turn parallel tool calls from racing on
  `/employee-profile.md`.

The slot feature is enabled by default and can be disabled without code changes:

```bash
AGENTSEEK_ENTERPRISE_MEMORY_SLOT_SUPERSESSION_ENABLED=false
```

When disabled, slot handling falls back to the P0/P3 behavior. Existing
slot-less profiles remain readable. The GA does not retroactively slot or
compact old memories; historical contradictions still require a later
compaction pass.

The profile write lock is process-local. It is sufficient for the current
single-gateway deployment. If the gateway is scaled to multiple processes or
hosts, add a database-level advisory lock or true upsert before treating durable
memory writes as cross-process safe.

## Release And Mirrors

| Location | Purpose | URL / ref |
| --- | --- | --- |
| GitHub tag | External collaboration and immutable source ref | `https://github.com/sambazhu/agentseek-enterprise/tree/enterprise-wecom-v0.0.9-ga` |
| GitHub repository | Upstream-facing fork and source of truth for development | `https://github.com/sambazhu/agentseek-enterprise` |
| Company GitLab mirror | Internal production mirror | `http://172.200.6.12:9091/zhuchunlin/agentseek-enterprise.git` |

Published refs:

| Ref | Commit | Use |
| --- | --- | --- |
| `production` | `8128aac4c37a46264477709adf07bd99e5eadb58` after final fast-forward | Recommended production deployment branch |
| `enterprise-wecom-v0.0.9-ga` | `8128aac4c37a46264477709adf07bd99e5eadb58` | Final immutable v0.0.9 runtime deployment tag |
| `enterprise-wecom-v0.0.9-rc1` | `8128aac4c37a46264477709adf07bd99e5eadb58` | Audited release-candidate ref, kept immutable |
| `enterprise-wecom-v0.0.8-ga` | `5833571` | Previous GA deployment and rollback tag |
| `enterprise-wecom-v0.0.7-ga` | `0485453` | Previous GA deployment tag, kept for audit |
| `enterprise-wecom-v0.0.6-ga-20260702` | `7b442a5` | Previous GA deployment tag, kept for audit |
| `enterprise-wecom-v0.0.5-ga-20260630` | `5cce3a2` | Previous GA deployment tag, kept for audit |
| `enterprise-wecom-v0.0.4-ga-20260629` | `1b06692` | Previous GA deployment tag, kept for audit |
| `enterprise-wecom-v0.0.4-prod-20260629` | `6cd8d41` | Earlier example-only freeze tag, kept for audit |

Runtime deployments should pin `enterprise-wecom-v0.0.9-ga`. Teams that want
the latest deployment instructions can clone `production` and then check out
the GA tag before starting the service.

Recommended GitLab project settings:

- default branch: `production`;
- protected branch: `production`;
- protected tags: `enterprise-wecom-v0.0.*`.

For production deployment from the company GitLab mirror:

```bash
git clone -b production http://172.200.6.12:9091/zhuchunlin/agentseek-enterprise.git
cd agentseek-enterprise
git checkout enterprise-wecom-v0.0.9-ga
```

For production deployment from GitHub:

```bash
git clone -b production https://github.com/sambazhu/agentseek-enterprise.git
cd agentseek-enterprise
git checkout enterprise-wecom-v0.0.9-ga
```

Do not commit deployment `.env`, `.agents/mcp.local.json`, runtime databases,
model files, or local virtual environments to either remote.

## Verified Capabilities

- In-repository example deployment passed live Mac mini smoke tests.
- `deepagents/enterprise-wecom` generated project passed standalone smoke tests
  in earlier GA validation and must be re-smoked after template changes.
- WeCom intelligent robot callback on port `12000`.
- Encrypted robot `open_userid` resolution through a self-built WeCom app.
- Employee identity lookup through DM JDBC in an isolated `sidecar` process.
- Employee identity cache with a 10 minute TTL.
- Per-session short-term memory persistence.
- Employee-scoped durable memory tools backed by SQLite or SQLAlchemy.
- Durable memory write-side near-duplicate deduplication and recall-side
  duplicate cleanup.
- Durable memory slot supersession with explicit contradiction notices for
  same-slot value changes.
- Per-employee durable memory profile write serialization for same-turn
  parallel memory tool calls.
- ContextSeek semantic long-term memory with PostgreSQL + pgvector.
- bge-m3 dense embeddings through ONNX Runtime and `tokenizers`; no torch in the
  gateway process.
- ContextSeek retrieval enrichment reaches the model prompt.
- Semantic memory is scoped by tenant and employee.
- PostgreSQL SCRAM authentication with a dedicated non-superuser gateway role.
- MCP lifecycle channel with gildata and Tavily tools.
- MCP policy/audit for read, write, and risky tool calls, with confirmation
  support for write/risky tools.
- WeCom retry deduplication by `msgid`.
- WeCom stream placeholder delivery for slow confirmed tool calls.
- LaunchAgent supervision with auto-restart.
- No SIGBUS with DM sidecar and pgvector/ONNX in the main gateway process.

## Required Runtime Settings

Keep these settings explicit in the deployment `.env`; do not rely on shell
defaults:

```bash
AGENTSEEK_ENV_FILE=examples/enterprise_wecom_digital_employee/.env
AGENTSEEK_STREAM_OUTPUT=true
AGENTSEEK_WECOM_ENABLED=true
AGENTSEEK_WECOM_PORT=12000
AGENTSEEK_WECOM_USERID_RESOLVE_MODE=openuserid_to_userid
AGENTSEEK_IDENTITY_PROVIDER=dm
AGENTSEEK_IDENTITY_DM_EXECUTION_MODE=sidecar
AGENTSEEK_ENTERPRISE_IDENTITY_CACHE_ENABLED=true
AGENTSEEK_ENTERPRISE_IDENTITY_CACHE_TTL_SECONDS=600
AGENTSEEK_ENTERPRISE_MEMORY_ENABLED=true
AGENTSEEK_ENTERPRISE_MEMORY_SQLALCHEMY_URL=postgresql+psycopg://agentseek_app:<password>@localhost/agentseek
AGENTSEEK_ENTERPRISE_STORE_SQLALCHEMY_URL=postgresql+psycopg://agentseek_app:<password>@localhost/agentseek
AGENTSEEK_CTX_STORAGE_BACKEND=pgvector
AGENTSEEK_CTX_PGVECTOR_URL=postgresql+psycopg://agentseek_app:<password>@localhost/agentseek
AGENTSEEK_CTX_PGVECTOR_TABLE=contextseek_pgvector_items
AGENTSEEK_CTX_PGVECTOR_DIMS=1024
AGENTSEEK_CTX_BGE_M3_ONNX_MODEL_PATH=./models/bge-m3-onnx/model.onnx
AGENTSEEK_CTX_BGE_M3_TOKENIZER_PATH=./models/bge-m3-onnx/tokenizer.json
AGENTSEEK_CTX_SCOPE_MODE=enterprise_user
AGENTSEEK_CTX_INJECTION_MODE=state
AGENTSEEK_CTX_RETRIEVAL_RECALL_ROUTES=["vector"]
LANGSMITH_TRACING=false
```

The actual `.env` also needs model credentials, WeCom callback credentials,
self-built WeCom app credentials, DM credentials, namespace secret, and MCP
server configuration. Keep those values out of git.

## Start Command

From the repository root:

```bash
examples/enterprise_wecom_digital_employee/scripts/run_gateway.sh
```

For LaunchAgent deployment, install:

```text
examples/enterprise_wecom_digital_employee/launchd/com.local.agentseek-enterprise-wecom.plist
```

Then manage it with:

```bash
launchctl unload ~/Library/LaunchAgents/com.local.agentseek-enterprise-wecom.plist 2>/dev/null || true
launchctl load -w ~/Library/LaunchAgents/com.local.agentseek-enterprise-wecom.plist
```

## Preflight

Before switching traffic or after changing `.env`, run:

```bash
examples/enterprise_wecom_digital_employee/scripts/prod_check.py \
  --env-file examples/enterprise_wecom_digital_employee/.env
```

The preflight must pass before treating the deployment as production-ready.

## Live Smoke Test

After restart, verify these prompts from WeCom:

### A. Identity

```text
我是谁
```

Expected: resolves to the employee context, e.g. name, OA account,
organization path, role, and post.

### B. Short-Term Memory (Relational Store)

```text
帮我记一下，我明天下午去深圳出差
我刚才说我要去哪里？
```

Expected: recalls the recent trip from the short-term conversation memory
database configured by `AGENTSEEK_ENTERPRISE_MEMORY_SQLALCHEMY_URL` or the
local fallback `AGENTSEEK_ENTERPRISE_MEMORY_SQLITE_PATH`.

### C. Explicit Durable Memory (Employee Store)

```text
请长期记住：我偏好简洁、分点的回复方式
你记得我的回复偏好吗？
```

Expected: recalls the preference through the dedicated durable memory tools
backed by `AGENTSEEK_ENTERPRISE_STORE_SQLALCHEMY_URL` or the local fallback
`AGENTSEEK_ENTERPRISE_STORE_SQLITE_PATH`.

### D. Semantic Long-Term Memory (ContextSeek + pgvector)

```text
请长期记住：我的职责是负责数据架构工作
我的工作职责是什么？
```

Expected: ContextSeek retrieves the semantically related historical turn from
pgvector and the model answer includes the data-architecture responsibility.

### E. Isolation

Use another employee account and ask for the first employee's semantic memory.
Expected: no cross-employee recall.

### F. MCP

```text
列一下当前可用的 MCP 工具
```

Expected: lists configured MCP services and tools.

When reading logs, use enough trailing context for multi-line model replies.
Avoid `grep | tail -1` for `content=` lines; use `grep -A N`.

## Rollback

If the DM sidecar becomes stale or unstable:

```bash
AGENTSEEK_IDENTITY_DM_EXECUTION_MODE=subprocess
```

If pgvector setup blocks service continuity and semantic recall can be
temporarily disabled:

```bash
AGENTSEEK_CTX_STORAGE_BACKEND=memory
```

If PostgreSQL + pgvector is unavailable but local semantic recall is acceptable:

```bash
AGENTSEEK_CTX_STORAGE_BACKEND=seekdb
AGENTSEEK_CTX_SEEKDB_PATH=./runtime/contextseek
```

These rollbacks preserve the gateway and identity path but reduce semantic
memory durability or move it back to local disk. Restore `sidecar` + `pgvector`
after investigation.

## Change Control

Any code change after this freeze needs at least:

- local regression tests for `agentseek-enterprise`, `agentseek-wecom`,
  `agentseek-contextseek`, and `agentseek-langchain`;
- Mac mini live smoke test for identity, short-term memory, ContextSeek
  retrieval, isolation, MCP, and WeCom retry behavior;
- a rendered-template standalone smoke test when template files change;
- a new freeze entry or tag if it changes production runtime behavior.
