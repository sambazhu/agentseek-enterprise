---
title: Templates
type: reference
audience: [A1, A2]
runs: no
verified_on: 2026-07-28
sources:
  - src/agentseek/data/catalog-lock.json
  - src/agentseek/cli/catalog.py
  - src/agentseek/cli/commands/create.py
  - https://github.com/agentseek-ai/agentseek-templates/releases/tag/v0.1.0
  - templates/deepagents/enterprise-wecom/README.md
  - templates/deepagents/enterprise-wecom/{{cookiecutter.project_slug}}/.env.example
  - contrib/agentseek-enterprise/src/agentseek_enterprise/mcp_policy.py
  - contrib/agentseek-enterprise/src/agentseek_enterprise/observability.py
---

# Templates

## Default Catalog

AgentSeek 0.1.0 uses the standalone
[`agentseek-ai/agentseek-templates`](https://github.com/agentseek-ai/agentseek-templates)
catalog. The installed wheel embeds an immutable lock with these coordinates:

| Coordinate | Value |
| --- | --- |
| Catalog release | `v0.1.0` |
| Catalog commit | `494863bc1b9aab19f9885d716c03ce654fb26014` |
| Lifecycle version | `2` |
| Core dependency snapshot | `core-snapshot-v0.1.0` |
| Core dependency commit | `883addad1e2993c4be6fc8ba053f87f25fb5057a` |

Listing, filtering, and interactive selection read the registry snapshot from
the wheel and work without downloading the catalog. Describing or generating a
named template fetches the repository archive at the exact catalog commit, then
validates its bytes against the trusted subtree digest in the wheel and
atomically caches only that selected template. A partial, stale, tampered, or
mismatched cache is not reused.

The default resolver never selects the lifecycle-v1 mirror in the core source
checkout and never falls back to mutable `main`. The core mirror remains only
for published 0.0.x clients and explicit local-path use.

## Available Templates

| Template | Description |
| --- | --- |
| `bub/default` | Lightweight Bub agent with AgentSeek lifecycle spec. |
| `deepagents/content-builder` | DeepAgents content builder with writing workflows, image generation, local UI, and AgentSeek lifecycle spec. |
| `deepagents/default` | Minimal DeepAgents app with AgentSeek lifecycle spec. |
| `deepagents/enterprise-wecom` | Enterprise WeCom digital employee with employee identity, governed MCP capabilities, pgvector semantic memory, enterprise events, WorkItem contracts, and signed-link delivery. This fork-local lifecycle-v1 template is not part of the default upstream catalog. |
| `deepagents/mcp` | DeepAgents MCP Tools app with validated stdio/HTTP configuration, a local calculator example, streamed UI, and AgentSeek lifecycle spec. |
| `deepagents/research` | DeepAgents research app with search workflow, local UI, and AgentSeek lifecycle spec. |
| `deepagents/sandbox` | DeepAgents sandbox coding agent with Daytona by default, a charged LangSmith Sandbox alternative, local UI, and AgentSeek lifecycle spec. |
| `langchain/agentic-rag` | LangChain agentic RAG with OceanBase vector search and AgentSeek lifecycle spec. |
| `langchain/agentic-rag-hybrid` | LangChain agentic hybrid RAG with image ingestion, vector/sparse/full-text/metadata search, comparison demos, optional Phoenix observability, and AgentSeek lifecycle spec. |
| `langchain/agentic-rag-openvino` | LangChain local RAG with OpenVINO models and AgentSeek lifecycle spec. |
| `langchain/cli-remote` | LangChain template for connecting the local lifecycle workflow to a remote LangGraph service. |
| `langchain/default` | LangChain agent app with local web UI and AgentSeek lifecycle spec. |
| `langchain/markdown-messages` | LangChain chat app with markdown message rendering and AgentSeek lifecycle spec. |

## Template Specs

| Form | Example |
| --- | --- |
| Type | `bub` |
| Type and name | `bub/default` |
| Absolute local path | `/path/to/template` |
| Git URL | `https://github.com/example/templates.git` |

## Catalog Repository Override

| Form | Result |
| --- | --- |
| `agentseek create --template-repo <https-url> --checkout <sha> --list-templates` | List the explicit AgentSeek catalog at the specified commit. |
| `agentseek create --template-repo <https-url> --checkout <sha> --filter rag --list-templates` | Filter that same explicit catalog commit. |
| `agentseek create langchain/default --template-repo <https-url> --checkout <sha> --describe` | Describe the named template at that same explicit catalog commit. |
| `agentseek create langchain/default --template-repo <https-url> --checkout <sha>` | Generate from that same explicit catalog commit. |

`<https-url>` identifies an AgentSeek catalog repository that contains
`templates/index.json`. `<sha>` must be a full 40-character lowercase Git commit
SHA matching `[0-9a-f]{40}`. The explicit catalog cannot be combined with a
positional direct Cookiecutter URL or absolute path. The positional URL/path
passthrough remains unchanged; only `--template-repo` is HTTPS-only.

The normalized catalog URL and exact commit identify the cache entry. AgentSeek
validates cache metadata before reuse. A failure for an explicit catalog does
not fall back to bundled templates or a local checkout.

Listing, filtering, and describing do not execute Cookiecutter hooks.
Generation trusts template content and may execute its hooks. The generated
`_agentseek_source_url` remains the AgentSeek core repository rather than the
catalog repository.

## Selection And Discovery

| Command | Result |
| --- | --- |
| `agentseek create` | Select the type and template interactively. |
| `agentseek create --list-templates` | List all known templates. |
| `agentseek create --list-templates --filter rag` | List only templates whose spec or description matches `rag`. |
| `agentseek create bub --list-templates` | List only `bub` templates. |
| `agentseek create bub` | Resolve to `bub/default`. |
| `agentseek create bub/default` | Use the specific template. |
| `agentseek create bub --template default` | Use `bub/default`. |
| `agentseek create bub/default --output-dir ./generated` | Write the generated project below the selected directory. |
| `agentseek create --template` | Compatibility entry point that lists templates. Prefer `--list-templates` in new scripts. |

## Enterprise WeCom Fork Template

`deepagents/enterprise-wecom` remains a fork-local lifecycle-v1 template while
the enterprise catalog boundary is prepared. Create it from an explicit local
checkout instead of assuming that it exists in the locked upstream catalog:

```bash
agentseek create ./templates/deepagents/enterprise-wecom
```

| Field | Value |
| --- | --- |
| Runtime | DeepAgents through `agentseek-langchain` and `bub gateway` |
| Channel | WeCom intelligent robot callback through `agentseek-wecom` |
| Identity | Enterprise employee context through `agentseek-enterprise` |
| Capabilities | Profile-scoped file, knowledge, and fixed MCP business adapters |
| Governance | MCP policy/audit, WorkItem contracts, confirmation gates, approval, publication, and delivery ledgers |
| Observability | Redacted enterprise JSONL events with optional Langfuse export |
| Memory | Session memory, explicit durable memory, and employee-scoped semantic memory |
| Delivery | Content-addressed DOCX plus one-time signed-link delivery |

The generated project includes `.agentseek/lifecycle.toml`,
`scripts/run_gateway.sh`, `scripts/bub_gateway.py`, `scripts/prod_check.py`, a
macOS LaunchAgent template, and `vendor/dameng/` for the DM JDBC driver.
