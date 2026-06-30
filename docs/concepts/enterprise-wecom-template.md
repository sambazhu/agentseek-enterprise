---
title: Enterprise WeCom Template
type: explanation
audience: [A2, A4]
runs: no
verified_on: 2026-06-30
sources:
  - templates/deepagents/enterprise-wecom/README.md
  - templates/deepagents/enterprise-wecom/{{cookiecutter.project_slug}}/.env.example
  - examples/enterprise_wecom_digital_employee/PRODUCTION_FREEZE.md
  - examples/enterprise_wecom_digital_employee/DEPLOYMENT_NOTES.md
---

# Enterprise WeCom Template

The `deepagents/enterprise-wecom` template builds an enterprise WeCom digital
employee around AgentSeek gateway, DeepAgents, employee identity, MCP tools, and
layered memory.

## Current Status

`enterprise-wecom-v0.0.4-ga-20260629` is the first GA baseline.

It was verified in two forms:

1. the in-repository `examples/enterprise_wecom_digital_employee` deployment;
2. a standalone project rendered by `agentseek create deepagents/enterprise-wecom`.

Both passed the same live WeCom smoke tests: identity, short-term memory,
explicit durable memory, semantic memory, MCP tools, sidecar stability, and
WeCom retry deduplication.

## Runtime Shape

```text
WeCom intelligent robot
-> agentseek-wecom channel
-> bub gateway
-> agentseek-enterprise employee identity
-> agentseek-langchain RunnableSpec
-> DeepAgents agent
-> MCP tools
```

The generated project owns its runtime details through `.agentseek/lifecycle.toml`
and `scripts/run_gateway.sh`.

## Memory Layers

| Layer | Storage | Purpose |
| --- | --- | --- |
| Short-term memory | SQLite | Recent per-session conversation context |
| Explicit durable memory | SQLiteStore | Facts the employee explicitly asks the assistant to remember |
| Semantic memory | ContextSeek + SeekDB | Semantic recall of historical conversation turns |

These layers are intentionally separate. Short-term memory helps with recent
follow-ups. Explicit durable memory is controlled through memory tools.
Semantic memory retrieves relevant historical context without requiring the
agent to choose a specific file or note.

## Isolation Choices

The template avoids a shared host filesystem backend for DeepAgents. It uses a
read-only virtual filesystem for trusted instructions and skills, and maps
durable memory to an employee-scoped store.

DM identity lookup can run in `subprocess` or `sidecar` mode. Both keep
JPype/libjvm out of the main gateway process so ContextSeek SeekDB and ONNX can
run in the gateway process.

## Production Baseline

Use the GA tag for deployments:

```bash
git checkout enterprise-wecom-v0.0.4-ga-20260629
```

The detailed freeze record lives in
`examples/enterprise_wecom_digital_employee/PRODUCTION_FREEZE.md`.

