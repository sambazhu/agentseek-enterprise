# Enterprise WeCom Production Freeze

This document freezes the first production-ready baseline for the enterprise
WeCom digital employee runtime.

## Baseline

- Branch: `enterprise/wecom-mcp-policy-audit`
- Final GA tag: `enterprise-wecom-v0.0.6-ga-20260702`
- Final GA tag commit: tag target
- Final verified integration commit: `60d0155`
- Runtime implementation commit: `7b442a5`
- Previous GA tag: `enterprise-wecom-v0.0.5-ga-20260630`
- Previous GA commit: `5cce3a2`
- Previous production freeze tag: `enterprise-wecom-v0.0.4-prod-20260629`
- Previous production freeze commit: `6cd8d41`
- Initial full runtime verification commit: `0c63850`
- Verification host: company-network Mac mini
- Verification date: 2026-07-02

The final GA tag includes the v0.0.6 MCP policy/audit path, WeCom stream
placeholder delivery fix, durable employee memory dedup/slot supersession, and
durable-memory concurrent-write serialization. Runtime code must remain
equivalent to the GA tag target unless a new freeze is created. The older
v0.0.5 and v0.0.4 tags are kept immutable for audit history; use the v0.0.6 GA
tag for new deployments.

## v0.0.6 Memory And MCP Runtime

This GA includes the v0.0.6 MCP policy/audit and WeCom stream fixes, plus the
durable employee memory slot-supersession upgrade and the follow-up concurrency
fix validated by Mac mini.

Durable employee memory now has these production-verified layers:

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

When disabled, slot handling falls back to the P0/P3 behavior verified in
`enterprise/memory-dedup`. Existing slot-less profiles remain readable. The GA
does not retroactively slot or compact old memories; historical contradictions
such as old 北京/深圳 travel entries still require a later P4 compaction pass.

The profile write lock is process-local. It is sufficient for the current
single-gateway deployment. If the gateway is scaled to multiple processes or
hosts, add a database-level advisory lock or true upsert before treating durable
memory writes as cross-process safe.

## Release And Mirrors

| Location | Purpose | URL / ref |
| --- | --- | --- |
| GitHub tag | External collaboration and immutable source ref | `https://github.com/sambazhu/agentseek-enterprise/tree/enterprise-wecom-v0.0.6-ga-20260702` |
| GitHub repository | Upstream-facing fork and source of truth for development | `https://github.com/sambazhu/agentseek-enterprise` |
| Company GitLab mirror | Internal production mirror | `http://172.200.6.12:9091/zhuchunlin/agentseek-enterprise.git` |

Published refs:

| Ref | Commit | Use |
| --- | --- | --- |
| `enterprise/wecom-mcp-policy-audit` | tag target | Active internal production branch |
| `enterprise-wecom-v0.0.6-ga-20260702` | tag target | Final immutable GA deployment tag |
| `enterprise-wecom-v0.0.6-rc2-memory-slots` | `2c626ce` | Previous RC with memory slot supersession, kept for audit |
| `enterprise-wecom-v0.0.5-ga-20260630` | `5cce3a2` | Previous GA deployment tag, kept for audit |
| `enterprise-wecom-v0.0.4-ga-20260629` | `1b06692` | Previous GA deployment tag, kept for audit |
| `enterprise-wecom-v0.0.4-prod-20260629` | `6cd8d41` | Earlier example-only freeze tag, kept for audit |

The branch may receive documentation-only updates after GA. Runtime deployments
should still pin `enterprise-wecom-v0.0.6-ga-20260702` unless a new GA tag is
created.

Recommended GitLab project settings:

- default branch: `enterprise/wecom-mcp-policy-audit`;
- protected branch: `enterprise/wecom-mcp-policy-audit`;
- protected tags: `enterprise-wecom-v0.0.*`.

For production deployment from the company GitLab mirror:

```bash
git clone http://172.200.6.12:9091/zhuchunlin/agentseek-enterprise.git
cd agentseek-enterprise
git checkout enterprise-wecom-v0.0.6-ga-20260702
```

For production deployment from GitHub:

```bash
git clone https://github.com/sambazhu/agentseek-enterprise.git
cd agentseek-enterprise
git checkout enterprise-wecom-v0.0.6-ga-20260702
```

Do not commit deployment `.env`, `.agents/mcp.local.json`, runtime databases,
or local virtual environments to either remote.

## Verified Capabilities

- In-repository example deployment and a separately rendered standalone project
  from `deepagents/enterprise-wecom` both passed the same live smoke tests.
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
- ContextSeek semantic long-term memory with local SeekDB.
- ContextSeek retrieval enrichment reaches the model prompt.
- MCP lifecycle channel with gildata and Tavily tools.
- MCP policy/audit for read, write, and risky tool calls, with confirmation
  support for write/risky tools.
- WeCom retry deduplication by `msgid`.
- WeCom stream placeholder delivery for slow confirmed tool calls.
- LaunchAgent supervision with auto-restart.
- No SIGBUS with DM sidecar and SeekDB/ONNX in the main gateway process.

The v0.0.6 baseline keeps local SQLite fallback for short-term memory and
explicit durable memory, and can move those two relational layers to
PostgreSQL/MySQL by setting `AGENTSEEK_ENTERPRISE_MEMORY_SQLALCHEMY_URL` and
`AGENTSEEK_ENTERPRISE_STORE_SQLALCHEMY_URL`. ContextSeek semantic memory remains
configured separately through its own backend settings.

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
AGENTSEEK_CTX_STORAGE_BACKEND=seekdb
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

### D. Semantic Long-Term Memory (ContextSeek + SeekDB)

```text
请长期记住：我的职责是负责数据架构工作
我的工作职责是什么？
```

Expected: ContextSeek retrieves the semantically related historical turn from
SeekDB and the model answer includes the data-architecture responsibility.

### E. MCP

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

If ContextSeek/SeekDB causes startup failures and service continuity is more
important than semantic recall:

```bash
AGENTSEEK_CTX_STORAGE_BACKEND=memory
```

These rollbacks preserve the gateway and identity path but reduce performance
or semantic-memory durability. Restore `sidecar` + `seekdb` after investigation.

## Change Control

Any code change after this freeze needs at least:

- local regression tests for `agentseek-enterprise`, `agentseek-wecom`,
  `agentseek-contextseek`, and `agentseek-langchain`;
- Mac mini live smoke test for identity, short-term memory, ContextSeek
  retrieval, MCP, and WeCom retry behavior;
- a new freeze entry or tag if it changes production runtime behavior.
