# agentseek-enterprise

## At A Glance

| Item | Value |
| --- | --- |
| Distribution | `agentseek-enterprise` |
| Python package | `agentseek_enterprise` |
| Bub entry point | `enterprise` |
| Workspace path | `contrib/agentseek-enterprise` |
| Test target | `make test-enterprise` |
| Type check target | `make typecheck-enterprise` |

## When To Use It

Use this plugin for enterprise-specific runtime context that should not live in the AgentSeek core package. Current capabilities include employee identity enrichment, per-session short-term memory, user-scoped persistent memory, and MCP policy helpers:

- reads an OA / WeCom user id from the inbound message envelope;
- resolves it through the configured employee identity provider;
- stores the normalized result in `state["employee_context"]` for LangChain, DeepAgents, skills, and MCP-aware templates.
- stores recent user/assistant turns in SQLite and reloads them as `state["short_term_memory"]`.
- creates non-PII LangGraph runtime context and resolves `StoreBackend` files into a per-tenant/per-employee SQLite namespace.
- evaluates local MCP allowlist/denylist policy and writes redacted MCP audit events when used by a template adapter.

This package owns runtime context and reusable enterprise guardrails. Channel
protocol handling belongs in a channel plugin such as `agentseek-wecom`, and
business actions such as meeting room booking or travel requests should stay in
MCP tools. The `enterprise-wecom` template wires the MCP policy helper into its
generated `call_mcp_tool` adapter.

## Install

In this monorepo, install the plugin group:

```bash
uv sync --group plugins
```

## Configuration

Enable the identity provider with:

```env
AGENTSEEK_IDENTITY_PROVIDER=dm
AGENTSEEK_IDENTITY_DM_HOST=127.0.0.1
AGENTSEEK_IDENTITY_DM_PORT=5236
AGENTSEEK_IDENTITY_DM_USER=readonly_user
AGENTSEEK_IDENTITY_DM_PASSWORD=
AGENTSEEK_IDENTITY_DM_SCHEMA=DBO
AGENTSEEK_IDENTITY_DM_DRIVER_MODULE=dmPython
AGENTSEEK_IDENTITY_DM_PARAMSTYLE=qmark
```

For Mac local debugging, where `dmPython` wheels may be unavailable, use the JDBC bridge:

```env
AGENTSEEK_IDENTITY_DM_DRIVER_MODULE=agentseek_enterprise.identity.jdbc_driver
AGENTSEEK_IDENTITY_DM_EXECUTION_MODE=subprocess
AGENTSEEK_IDENTITY_DM_SUBPROCESS_TIMEOUT_SECONDS=30
AGENTSEEK_ENTERPRISE_IDENTITY_CACHE_ENABLED=true
AGENTSEEK_ENTERPRISE_IDENTITY_CACHE_TTL_SECONDS=600
AGENTSEEK_ENTERPRISE_IDENTITY_CACHE_MAX_ENTRIES=1024
AGENTSEEK_IDENTITY_DM_JDBC_JAR=vendor/dameng/DmJdbcDriver18-8.1.3.62.jar
AGENTSEEK_IDENTITY_DM_JDBC_CLASS=dm.jdbc.driver.DmDriver
AGENTSEEK_IDENTITY_DM_JDBC_JAVA_HOME=/opt/homebrew/opt/openjdk@11/libexec/openjdk.jdk/Contents/Home
```

`AGENTSEEK_IDENTITY_DM_EXECUTION_MODE=subprocess` runs the JDBC lookup in a
short-lived Python child process. The main gateway process does not load JPype
or `libjvm`, so it can coexist with vector/ONNX runtimes such as ContextSeek
SeekDB.

`AGENTSEEK_IDENTITY_DM_EXECUTION_MODE=sidecar` uses the same isolation boundary,
but keeps one local JSON-lines worker alive behind the gateway process. The
worker holds the DM/JDBC connection and serves lookups over stdin/stdout, so it
does not open a network port and still keeps `libjvm` out of the gateway
process. `AGENTSEEK_IDENTITY_DM_SUBPROCESS_TIMEOUT_SECONDS` also controls each
sidecar request timeout.

The optional identity cache stores successful `EmployeeContext` lookups in the
gateway process for a short TTL. It does not cache lookup failures or missing
employees, so temporary DM issues and newly synced users can recover on the next
request.

The plugin only performs identity lookup when `AGENTSEEK_IDENTITY_PROVIDER=dm`, or when `AGENTSEEK_ENTERPRISE_IDENTITY_ENABLED=true` is set explicitly.

Enable short-term memory with:

```env
AGENTSEEK_ENTERPRISE_MEMORY_ENABLED=true
# Production: set a SQLAlchemy URL to move this store to PostgreSQL/MySQL.
# AGENTSEEK_ENTERPRISE_MEMORY_SQLALCHEMY_URL=postgresql+psycopg://user:pass@host:5432/agentseek
# AGENTSEEK_ENTERPRISE_MEMORY_SQLALCHEMY_URL=mysql+pymysql://user:pass@host:3306/agentseek?charset=utf8mb4
# Local fallback when the SQLAlchemy URL is empty:
AGENTSEEK_ENTERPRISE_MEMORY_SQLITE_PATH=./runtime/enterprise-short-term-memory.sqlite3
AGENTSEEK_ENTERPRISE_MEMORY_RECENT_TURNS=8
AGENTSEEK_ENTERPRISE_MEMORY_TTL_SECONDS=604800
AGENTSEEK_ENTERPRISE_MEMORY_MAX_CONTENT_CHARS=4000
```

The enterprise DeepAgents template also uses a separate durable StoreBackend:

```env
AGENTSEEK_ENTERPRISE_TENANT_ID=wkzq
# Use a high-entropy value in production. Without it, namespace keys are SHA-256 digests.
AGENTSEEK_ENTERPRISE_NAMESPACE_SECRET=
# Production: set a SQLAlchemy URL to move explicit durable memory to PostgreSQL/MySQL.
# AGENTSEEK_ENTERPRISE_STORE_SQLALCHEMY_URL=postgresql+psycopg://user:pass@host:5432/agentseek
# AGENTSEEK_ENTERPRISE_STORE_SQLALCHEMY_URL=mysql+pymysql://user:pass@host:3306/agentseek?charset=utf8mb4
# Local fallback when the SQLAlchemy URL is empty:
AGENTSEEK_ENTERPRISE_STORE_SQLITE_PATH=./runtime/enterprise-long-term-store.sqlite3
```

The template does not grant generic filesystem tools access to `/memories`. Only its narrow
`remember_employee_memory`, `recall_employee_memory`, and `forget_employee_memory` tools can
reach the authenticated employee's namespace. This keeps durable preferences and work context
isolated while avoiding arbitrary prompt content becoming a writable filesystem.

Configure MCP policy and audit for template adapters with:

```env
AGENTSEEK_ENTERPRISE_MCP_POLICY_ENABLED=true
AGENTSEEK_ENTERPRISE_MCP_DEFAULT_ACTION=allow
AGENTSEEK_ENTERPRISE_MCP_ALLOWLIST=gildata_datamap-*/*,tavily-search/*
AGENTSEEK_ENTERPRISE_MCP_DENYLIST=
AGENTSEEK_ENTERPRISE_MCP_WRITE_TOOLS=office/book_room,oa/submit_travel
AGENTSEEK_ENTERPRISE_MCP_RISKY_TOOLS=oa/cancel_request
AGENTSEEK_ENTERPRISE_MCP_CONFIRM_TOOLS=
AGENTSEEK_ENTERPRISE_MCP_REQUIRE_CONFIRMATION=true
AGENTSEEK_ENTERPRISE_MCP_AUDIT_ENABLED=true
AGENTSEEK_ENTERPRISE_MCP_AUDIT_LOG_PATH=./runtime/mcp-audit.jsonl
```

Tool patterns accept `server/tool`, `server:tool`, or wildcards such as
`gildata_datamap-*/*`. Read/query tools are allowed by default. Tools classified
as write, risky, or confirmation-required return a confirmation-required result
until the adapter calls them again with `confirmed=true`. Audit JSONL records
the action, tool reference, risk, policy reason, and redacted arguments.

MCP policy is evaluated locally before the template adapter opens the remote MCP
client. The policy result has two dimensions:

| Dimension | Values | Meaning |
| --- | --- | --- |
| Risk | `read`, `write`, `risky` | Query-only, state-changing, or high-risk business operation. |
| Action | `allow`, `deny`, `confirm` | Execute now, block before remote call, or require employee confirmation. |

The evaluation order is:

1. `AGENTSEEK_ENTERPRISE_MCP_DENYLIST` blocks matching tools.
2. Non-empty `AGENTSEEK_ENTERPRISE_MCP_ALLOWLIST` blocks tools outside the list.
3. `AGENTSEEK_ENTERPRISE_MCP_DEFAULT_ACTION=deny` blocks tools not explicitly allowed.
4. `AGENTSEEK_ENTERPRISE_MCP_RISKY_TOOLS` marks matching tools as `risky`.
5. `AGENTSEEK_ENTERPRISE_MCP_WRITE_TOOLS` marks matching tools as `write`.
6. `AGENTSEEK_ENTERPRISE_MCP_CONFIRM_TOOLS`, `write`, and `risky` tools require
   confirmation when `AGENTSEEK_ENTERPRISE_MCP_REQUIRE_CONFIRMATION=true`.

The `enterprise-wecom` adapter writes audit events for `denied`,
`confirmation_required`, `failed`, and `succeeded` outcomes. Each event includes
`timestamp`, `server_name`, `tool_name`, `tool_ref`, `action`, `risk`,
`confirmed`, `reason`, redacted `arguments`, and a truncated `result_summary` or
error. Argument keys containing password, secret, token, api key, private key,
credential, `身份证`, `银行卡`, `密码`, `密钥`, or `令牌` are replaced with
`[REDACTED]`.

For broad production use, start with default `allow` while inventorying tools.
After the inventory is stable, switch to:

```env
AGENTSEEK_ENTERPRISE_MCP_DEFAULT_ACTION=deny
AGENTSEEK_ENTERPRISE_MCP_ALLOWLIST=gildata_datamap-*/*,tavily-search/tavily_search,office/*,oa/*
AGENTSEEK_ENTERPRISE_MCP_WRITE_TOOLS=office/book_room,oa/submit_travel
AGENTSEEK_ENTERPRISE_MCP_RISKY_TOOLS=oa/cancel_request,agent-platform/install_agent_skills
AGENTSEEK_ENTERPRISE_MCP_CONFIRM_TOOLS=tavily-search/tavily_search
```

This keeps known query tools available, requires confirmation for business
writes, and can force confirmation for external search even when the tool is
query-only.

## Runtime Behavior

The plugin implements `load_state()` and adds:

- `employee_context`: normalized public employee identity for agent templates and graph state;
- `_employee_identity`: internal lookup status, source, and diagnostics.
- `short_term_memory`: recent messages for the same `session_id`, when enabled.
- `_langgraph_runtime_context`: non-model state carrying tenant, employee, and session digest keys for LangGraph runtime-aware components.

The plugin also exposes a disabled-by-default `system_prompt()` hook. Set `AGENTSEEK_ENTERPRISE_IDENTITY_SYSTEM_PROMPT=true` and/or `AGENTSEEK_ENTERPRISE_MEMORY_SYSTEM_PROMPT=true` only when the model adapter consumes Bub system prompts. The `enterprise-wecom` template injects these state blocks into its LangChain message list directly.

## Verify

```bash
make test-enterprise
make typecheck-enterprise
```

For a real DM identity probe:

```bash
uv run --with jaydebeapi --with JPype1 python scripts/probe_staff_identity.py --oa <oa_account> --source python-db
```

## Limitations

- The Mac JDBC bridge requires a local JDK plus `jaydebeapi` and `JPype1`.
- On macOS, prefer `AGENTSEEK_IDENTITY_DM_EXECUTION_MODE=subprocess` or
  `sidecar` when the gateway also loads ONNX/vector runtimes in the main
  process. Use `subprocess` as the conservative rollback mode; use `sidecar`
  after validating the long-lived worker in the target network environment.
- The identity provider currently resolves employees by OA / WeCom userid only.
- `SQLiteStore` is a deterministic persistent store, not semantic/vector retrieval. A production OceanBase or vector-store adapter should implement the same LangGraph `BaseStore` interface rather than reuse the Bub TapeStore interface.
