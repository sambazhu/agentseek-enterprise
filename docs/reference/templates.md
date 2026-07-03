---
title: Templates
type: reference
audience: [A1, A2]
runs: no
verified_on: 2026-07-03
sources:
  - templates/index.json
  - src/agentseek/cli/commands/create.py
  - templates/deepagents/enterprise-wecom/README.md
  - templates/deepagents/enterprise-wecom/{{cookiecutter.project_slug}}/.env.example
  - contrib/agentseek-enterprise/src/agentseek_enterprise/mcp_policy.py
---

# Templates

## Available Templates

| Template | Description |
| --- | --- |
| `bub/contextseek` | Bub agent with ContextSeek semantic memory and AgentSeek lifecycle spec. |
| `bub/default` | Lightweight Bub agent with AgentSeek lifecycle spec. |
| `deepagents/content-builder` | DeepAgents content builder with writing workflows, image generation, local UI, and AgentSeek lifecycle spec. |
| `deepagents/default` | Minimal DeepAgents app with AgentSeek lifecycle spec. |
| `deepagents/enterprise-wecom` | Enterprise WeCom digital employee with employee identity, MCP tools, pgvector semantic memory, and AgentSeek lifecycle spec. |
| `deepagents/research` | DeepAgents research app with search workflow, local UI, and AgentSeek lifecycle spec. |
| `langchain/agentic-rag` | LangChain agentic RAG with OceanBase vector search and AgentSeek lifecycle spec. |
| `langchain/agentic-rag-openvino` | LangChain agentic RAG with local OpenVINO models and AgentSeek lifecycle spec. |
| `langchain/cli-remote` | LangChain template for connecting the local lifecycle workflow to a remote LangGraph service. |
| `langchain/default` | LangChain agent app with local web UI and AgentSeek lifecycle spec. |
| `langchain/markdown-messages` | LangChain chat app with markdown message rendering and AgentSeek lifecycle spec. |
| `langchain/sandbox` | Sandbox-backed coding agent with local UI and AgentSeek lifecycle spec. |

## Template Specs

| Form | Example |
| --- | --- |
| Type | `bub` |
| Type and name | `bub/default` |
| Absolute local path | `/path/to/template` |
| Git URL | `https://github.com/example/templates.git` |

## Selection And Discovery

| Command | Result |
| --- | --- |
| `agentseek create` | Select the type and template interactively. |
| `agentseek create --list-templates` | List all known templates. |
| `agentseek create bub --list-templates` | List only `bub` templates. |
| `agentseek create bub` | Resolve to `bub/default`. |
| `agentseek create bub/default` | Use the specific template. |
| `agentseek create bub --template default` | Use `bub/default`. |
| `agentseek create --template` | Compatibility entry point that lists templates. Prefer `--list-templates` in new scripts. |

## Enterprise WeCom Template

| Field | Value |
| --- | --- |
| Template | `deepagents/enterprise-wecom` |
| Runtime | DeepAgents through `agentseek-langchain` and `bub gateway` |
| Channel | WeCom intelligent robot callback through `agentseek-wecom` |
| Identity | Enterprise employee context through `agentseek-enterprise` |
| Business tools | MCP servers from `.agents/mcp.json` |
| MCP policy | Local allowlist/denylist, write/risky confirmation, and JSONL audit |
| Short-term memory | Per-session SQLAlchemy memory, SQLite fallback |
| Explicit durable memory | Employee-scoped LangGraph Store memory tools, SQLAlchemy or SQLite fallback |
| Semantic memory | ContextSeek with PostgreSQL + pgvector in production; SeekDB fallback for local development |
| Production check | `scripts/prod_check.py --env-file .env` in the generated project |

Create form:

```bash
agentseek create deepagents/enterprise-wecom
```

The generated project includes `.agentseek/lifecycle.toml`, `scripts/run_gateway.sh`,
`scripts/bub_gateway.py`, `scripts/prod_check.py`, a macOS LaunchAgent template,
and a `vendor/dameng/` directory for the DM JDBC driver.

Enterprise WeCom MCP policy settings:

| Setting | Default | Meaning |
| --- | --- | --- |
| `AGENTSEEK_ENTERPRISE_MCP_POLICY_ENABLED` | `true` | Enable local MCP policy checks in the template adapter. |
| `AGENTSEEK_ENTERPRISE_MCP_DEFAULT_ACTION` | `allow` | Default action when no allowlist/denylist rule blocks the tool. |
| `AGENTSEEK_ENTERPRISE_MCP_ALLOWLIST` | empty | Optional comma-separated `server/tool` patterns allowed to run. |
| `AGENTSEEK_ENTERPRISE_MCP_DENYLIST` | empty | Comma-separated `server/tool` patterns blocked before remote MCP calls. |
| `AGENTSEEK_ENTERPRISE_MCP_WRITE_TOOLS` | empty | Comma-separated patterns classified as state-changing tools. |
| `AGENTSEEK_ENTERPRISE_MCP_RISKY_TOOLS` | empty | Comma-separated patterns classified as high-risk tools. |
| `AGENTSEEK_ENTERPRISE_MCP_CONFIRM_TOOLS` | empty | Comma-separated patterns that require confirmation even when classified as `read`. |
| `AGENTSEEK_ENTERPRISE_MCP_REQUIRE_CONFIRMATION` | `true` | Require explicit confirmation for `write`, `risky`, and confirm-listed tools. |
| `AGENTSEEK_ENTERPRISE_MCP_AUDIT_ENABLED` | `true` | Write MCP decision events to JSONL. |
| `AGENTSEEK_ENTERPRISE_MCP_AUDIT_LOG_PATH` | `./runtime/mcp-audit.jsonl` | Audit JSONL path relative to the project root unless absolute. |

See [Enterprise WeCom Template](../concepts/enterprise-wecom-template.md) for
the runtime and memory-layer design.
