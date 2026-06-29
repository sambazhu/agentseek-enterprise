# {{ cookiecutter.project_name }}

Enterprise WeCom digital employee scaffolded from `deepagents/enterprise-wecom`.

It runs a DeepAgents agent through AgentSeek gateway, receives WeCom intelligent robot callbacks, resolves the WeCom user to an employee context, and exposes business MCP tools from `.agents/mcp.json`.

The template injects `state["employee_context"]` and `state["short_term_memory"]` into the model-visible message list, so questions like `我是谁` and follow-ups like `我刚才说我要去哪里` can be answered from runtime context instead of asking the user to restate their OA account or prior message. It also configures a tenant-and-employee scoped persistent `StoreBackend` for explicitly requested durable preferences and work context, plus ContextSeek semantic recall across the same employee's sessions.

## Setup

```bash
uv sync
cp .env.example .env
```

Fill `.env` with:

- model provider credentials;
- WeCom callback `Token` and `EncodingAESKey`;
- self-built WeCom app `corp_id` and app secret;
- employee identity database settings. On macOS, copy the DM JDBC jar to
  `vendor/dameng/` and keep `AGENTSEEK_IDENTITY_DM_EXECUTION_MODE=subprocess`
  first; switch to `sidecar` after the subprocess path is stable;
- short-term memory retention settings;
- the tenant id, namespace secret, and durable store path;
- local ContextSeek SeekDB storage and its first-start embedding-model download;
- MCP servers in `.agents/mcp.json`.

## Run

```bash
scripts/run_gateway.sh
```

Before installing launchd, run the production preflight. It redacts secrets and
checks only presence, file paths, writable runtime directories, tracing intent,
identity isolation mode, and namespace-secret readiness:

```bash
scripts/prod_check.py --env-file .env
```

Generate a new namespace secret before formal production handoff:

```bash
scripts/prod_check.py --generate-namespace-secret
```

For macOS process supervision, edit `{{ cookiecutter.deployment_path }}` in
`launchd/com.local.{{cookiecutter.project_slug}}.plist` if needed, then install
it as a user LaunchAgent:

```bash
mkdir -p ~/Library/LaunchAgents
cp launchd/com.local.{{cookiecutter.project_slug}}.plist ~/Library/LaunchAgents/
launchctl unload ~/Library/LaunchAgents/com.local.{{cookiecutter.project_slug}}.plist 2>/dev/null || true
launchctl load -w ~/Library/LaunchAgents/com.local.{{cookiecutter.project_slug}}.plist
tail -f /tmp/{{cookiecutter.project_slug}}.log
```

The WeCom callback listens on port `{{ cookiecutter.wecom_port }}` and path:

```text
{{ cookiecutter.wecom_callback_path }}
```

## Smoke Test

After configuring WeCom, send `你好` to the intelligent robot. A healthy first response should show that:

- `userid` is converted from encrypted robot `open_userid` to plaintext WeCom userid;
- `oa_account` matches that plaintext userid;
- `employee_context` is present in runtime state.
- follow-up questions can use recent messages stored under `short_term_memory`.
- semantic retrieval is scoped to the resolved employee rather than the WeCom session.

For short-term memory, send:

```text
帮我记一下，我明天下午去深圳出差
我刚才说我要去哪里？
```

For MCP, add one server to `.agents/mcp.json`, restart the gateway, then ask:

```text
列一下当前可用的 MCP 工具
```

## What's Different Vs. Pure DeepAgents

- `src/{{ cookiecutter.project_slug }}/agent.py` exports `build_spec()` for `AGENTSEEK_LANGCHAIN_SPEC`.
- `src/{{ cookiecutter.project_slug }}/tools.py` adds a lightweight MCP list/call adapter.
- `AGENTS.md` and `skills/` carry enterprise identity and office workflow rules.
- DeepAgents uses an isolated `CompositeBackend`: only `AGENTS.md` and `skills/` are copied into a read-only virtual filesystem. Durable `/memories` storage is mapped to a tenant-and-employee scoped `StoreBackend`, but only dedicated memory tools can access it. The agent cannot read the project directory, `.env`, or other host paths, and cannot write files or execute local commands.
- ContextSeek only stores final conversation turns, not MCP calls or tool output. Retrieved history is marked as untrusted context and injected as a system message. SeekDB is the local vector backend; production storage is chosen through ContextSeek configuration, so it can later move to OceanBase or an adapter for Milvus without changing the employee scope contract.
- DM JDBC identity lookup can run in a short-lived subprocess or a persistent
  local sidecar process. Both keep JPype/libjvm out of the gateway process so
  SeekDB/ONNX can coexist with the DM driver. `subprocess` is the conservative
  rollback mode; `sidecar` avoids cold-starting the JVM on cache misses.
- Successful employee identity lookups can be cached briefly in the gateway
  process. Missing users and lookup errors are not cached.
- The WeCom channel deduplicates intelligent-robot retries by `msgid` and
  reuses the original stream response, so slow first replies do not launch
  duplicate agent turns.
- `pyproject.toml` depends on AgentSeek runtime plugins: `agentseek-langchain`, `agentseek-wecom`, `agentseek-enterprise`, `agentseek-schedule-sqlalchemy`, and `bub-mcp`.

Author: {{ cookiecutter.author }}
