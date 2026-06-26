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

Use this plugin for enterprise-specific runtime context that should not live in the AgentSeek core package. Current capabilities include employee identity enrichment and per-session short-term memory:

- reads an OA / WeCom user id from the inbound message envelope;
- resolves it through the configured employee identity provider;
- stores the normalized result in `state["employee_context"]` for LangChain, DeepAgents, skills, and MCP-aware templates.
- stores recent user/assistant turns in SQLite and reloads them as `state["short_term_memory"]`.

This package owns runtime context only. Channel protocol handling belongs in a channel plugin such as `agentseek-wecom`, and business actions such as meeting room booking or travel requests should stay in MCP tools.

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
AGENTSEEK_IDENTITY_DM_JDBC_JAR=vendor/dameng/DmJdbcDriver18-8.1.2.192.jar
AGENTSEEK_IDENTITY_DM_JDBC_CLASS=dm.jdbc.driver.DmDriver
AGENTSEEK_IDENTITY_DM_JDBC_JAVA_HOME=/path/to/jdk
```

The plugin only performs identity lookup when `AGENTSEEK_IDENTITY_PROVIDER=dm`, or when `AGENTSEEK_ENTERPRISE_IDENTITY_ENABLED=true` is set explicitly.

Enable short-term memory with:

```env
AGENTSEEK_ENTERPRISE_MEMORY_ENABLED=true
AGENTSEEK_ENTERPRISE_MEMORY_SQLITE_PATH=./runtime/enterprise-short-term-memory.sqlite3
AGENTSEEK_ENTERPRISE_MEMORY_RECENT_TURNS=8
AGENTSEEK_ENTERPRISE_MEMORY_TTL_SECONDS=604800
AGENTSEEK_ENTERPRISE_MEMORY_MAX_CONTENT_CHARS=4000
```

## Runtime Behavior

The plugin implements `load_state()` and adds:

- `employee_context`: normalized public employee identity for agent templates and graph state;
- `_employee_identity`: internal lookup status, source, and diagnostics.
- `short_term_memory`: recent messages for the same `session_id`, when enabled.

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
- The identity provider currently resolves employees by OA / WeCom userid only.
- Long-term semantic memory and WeCom callback handling are intentionally separate modules.
