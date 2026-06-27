# Enterprise WeCom Digital Employee

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
- employee identity database settings;
- short-term memory retention settings;
- the tenant id, namespace secret, and durable store path;
- local ContextSeek SeekDB storage and its first-start embedding-model download;
- MCP servers in `.agents/mcp.json`.

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
export PYTHONPATH="$PWD/examples/enterprise_wecom_digital_employee/src"
uv run agentseek gateway \
  --enable-channel wecom \
  --enable-channel mcp.lifecycle \
  --enable-channel skills.lifecycle
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

- `src/enterprise_wecom_digital_employee/agent.py` exports `build_spec()` for `AGENTSEEK_LANGCHAIN_SPEC`.
- `src/enterprise_wecom_digital_employee/tools.py` adds a lightweight MCP list/call adapter.
- `AGENTS.md` and `skills/` carry enterprise identity and office workflow rules.
- DeepAgents uses an isolated `CompositeBackend`: only `AGENTS.md` and `skills/` are copied into a read-only virtual filesystem. Durable `/memories` storage is mapped to a tenant-and-employee scoped `StoreBackend`, but only dedicated memory tools can access it. The agent cannot read the project directory, `.env`, or other host paths, and cannot write files or execute local commands.
- ContextSeek only stores final conversation turns, not MCP calls or tool output. Retrieved history is marked as untrusted context and injected as a system message. SeekDB is the local vector backend; production storage is chosen through ContextSeek configuration, so it can later move to OceanBase or an adapter for Milvus without changing the employee scope contract.
- `pyproject.toml` depends on AgentSeek runtime plugins: `agentseek-langchain`, `agentseek-wecom`, `agentseek-enterprise`, `agentseek-schedule-sqlalchemy`, and `bub-mcp`.

Author: Your Name
