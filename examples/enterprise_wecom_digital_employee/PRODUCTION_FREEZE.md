# Enterprise WeCom Production Freeze

This document freezes the first production-ready baseline for the enterprise
WeCom digital employee runtime.

## Baseline

- Branch: `enterprise/wecom-runtime-v0.0.4`
- Final GA tag: `enterprise-wecom-v0.0.4-ga-20260629`
- Final GA tag commit: documentation-only successor of `7409444`
- Final verified integration commit: `7409444`
- Previous production freeze tag: `enterprise-wecom-v0.0.4-prod-20260629`
- Previous production freeze commit: `6cd8d41`
- Initial full runtime verification commit: `0c63850`
- Verification host: company-network Mac mini
- Verification date: 2026-06-29

The final GA tag points to a documentation-only successor of `7409444`.
Runtime code must remain equivalent to `7409444` unless a new freeze is
created. The older `prod` tag is kept immutable for audit history; use the GA
tag for new deployments.

## Release And Mirrors

| Location | Purpose | URL / ref |
| --- | --- | --- |
| GitHub release | External collaboration and release notes | `https://github.com/sambazhu/agentseek-enterprise/releases/tag/enterprise-wecom-v0.0.4-ga-20260629` |
| GitHub repository | Upstream-facing fork and source of truth for development | `https://github.com/sambazhu/agentseek-enterprise` |
| Company GitLab mirror | Internal production mirror | `http://172.200.6.12:9091/zhuchunlin/agentseek-enterprise.git` |

Published refs:

| Ref | Commit | Use |
| --- | --- | --- |
| `enterprise/wecom-runtime-v0.0.4` | Documentation head; runtime equivalent to `1b06692` | Active internal production branch |
| `enterprise-wecom-v0.0.4-ga-20260629` | `1b06692` | Final immutable GA deployment tag |
| `enterprise-wecom-v0.0.4-prod-20260629` | `6cd8d41` | Earlier example-only freeze tag, kept for audit |

The branch may receive documentation-only updates after GA. Runtime deployments
should still pin `enterprise-wecom-v0.0.4-ga-20260629` unless a new GA tag is
created.

Recommended GitLab project settings:

- default branch: `enterprise/wecom-runtime-v0.0.4`;
- protected branch: `enterprise/wecom-runtime-v0.0.4`;
- protected tags: `enterprise-wecom-v0.0.4-*`.

For production deployment from the company GitLab mirror:

```bash
git clone http://172.200.6.12:9091/zhuchunlin/agentseek-enterprise.git
cd agentseek-enterprise
git checkout enterprise-wecom-v0.0.4-ga-20260629
```

For production deployment from GitHub:

```bash
git clone https://github.com/sambazhu/agentseek-enterprise.git
cd agentseek-enterprise
git checkout enterprise-wecom-v0.0.4-ga-20260629
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
- Employee-scoped durable memory tools backed by SQLiteStore.
- ContextSeek semantic long-term memory with local SeekDB.
- ContextSeek retrieval enrichment reaches the model prompt.
- MCP lifecycle channel with gildata and Tavily tools.
- WeCom retry deduplication by `msgid`.
- LaunchAgent supervision with auto-restart.
- No SIGBUS with DM sidecar and SeekDB/ONNX in the main gateway process.

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

### B. Short-Term Memory (SQLite)

```text
帮我记一下，我明天下午去深圳出差
我刚才说我要去哪里？
```

Expected: recalls the recent trip from the short-term conversation memory
database configured by `AGENTSEEK_ENTERPRISE_MEMORY_SQLITE_PATH`.

### C. Explicit Durable Memory (SQLiteStore)

```text
请长期记住：我偏好简洁、分点的回复方式
你记得我的回复偏好吗？
```

Expected: recalls the preference through the dedicated durable memory tools
backed by `AGENTSEEK_ENTERPRISE_STORE_SQLITE_PATH`.

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
