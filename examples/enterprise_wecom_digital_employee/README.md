# Enterprise WeCom Digital Employee

Enterprise WeCom digital employee scaffolded from `deepagents/enterprise-wecom`.

It runs a DeepAgents agent through AgentSeek gateway, receives WeCom intelligent robot callbacks, resolves the WeCom user to an employee context, and exposes business MCP tools from `.agents/mcp.json`.

The template injects `state["employee_context"]` and `state["short_term_memory"]` into the model-visible message list, so questions like `我是谁` and follow-ups like `我刚才说我要去哪里` can be answered from runtime context instead of asking the user to restate their OA account or prior message.

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
- MCP servers in `.agents/mcp.json`.

## Run

```bash
export PYTHONPATH=src
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
- `pyproject.toml` depends on AgentSeek runtime plugins: `agentseek-langchain`, `agentseek-wecom`, `agentseek-enterprise`, `agentseek-schedule-sqlalchemy`, and `bub-mcp`.

Author: Your Name
