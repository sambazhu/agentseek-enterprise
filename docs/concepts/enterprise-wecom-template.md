---
title: Enterprise WeCom Template
type: explanation
audience: [A2, A4]
runs: no
verified_on: 2026-07-08
sources:
  - templates/deepagents/enterprise-wecom/README.md
  - templates/deepagents/enterprise-wecom/{{cookiecutter.project_slug}}/.env.example
  - contrib/agentseek-enterprise/src/agentseek_enterprise/mcp_policy.py
  - examples/enterprise_wecom_digital_employee/PRODUCTION_FREEZE.md
  - examples/enterprise_wecom_digital_employee/DEPLOYMENT_NOTES.md
---

# Enterprise WeCom Template

The `deepagents/enterprise-wecom` template builds an enterprise WeCom digital
employee around AgentSeek gateway, DeepAgents, employee identity, MCP tools, and
layered memory.

## Current Status

`enterprise-wecom-v0.0.8-ga` is the current GA baseline.

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
| Short-term memory | SQLAlchemy URL; SQLite fallback for local development | Recent per-session conversation context |
| Explicit durable memory | Employee-scoped LangGraph Store; SQLAlchemy URL optional, SQLite fallback local | Facts the employee explicitly asks the assistant to remember |
| Semantic memory | ContextSeek + PostgreSQL + pgvector; SeekDB fallback for local development | Semantic recall of historical conversation turns |

These layers are intentionally separate. Short-term memory helps with recent
follow-ups. Explicit durable memory is controlled through memory tools.
Semantic memory retrieves relevant historical context without requiring the
agent to choose a specific file or note.

Production deployments can move the first two layers to PostgreSQL/MySQL:
`AGENTSEEK_ENTERPRISE_MEMORY_SQLALCHEMY_URL` controls short-term memory, and
`AGENTSEEK_ENTERPRISE_STORE_SQLALCHEMY_URL` controls explicit durable memory.
Semantic memory is controlled separately by ContextSeek backend settings. The
v0.0.8 production baseline uses `AGENTSEEK_CTX_STORAGE_BACKEND=pgvector`,
`AGENTSEEK_CTX_PGVECTOR_URL`, and bge-m3 ONNX embedding paths.

## Isolation Choices

The template avoids a shared host filesystem backend for DeepAgents. It uses a
read-only virtual filesystem for trusted instructions and skills, and maps
durable memory to an employee-scoped store.

DM identity lookup can run in `subprocess` or `sidecar` mode. Both keep
JPype/libjvm out of the main gateway process so pgvector embedding through ONNX
Runtime can run in the gateway process.

## MCP Policy And Audit

MCP tools are the boundary for business actions such as meeting-room booking,
travel requests, or data lookup. The generated `call_mcp_tool` adapter evaluates
a local policy before it calls a remote MCP server.

The policy separates read/query tools from write or risky tools. Read tools can
run by default. Write or risky tools can require an explicit employee
confirmation, and denied tools are blocked before any remote call is made.
Every adapter decision can be written to a redacted JSONL audit log.

The runtime does not try to put business authorization rules inside the model.
It gives the model one tool surface and keeps the enforcement step deterministic:
allowlist and denylist matching, risk classification, confirmation checks, and
audit logging all happen before the remote MCP call.

This creates a two-step flow for state-changing tools:

1. The model asks to call the tool without `confirmed=true`.
2. The adapter returns a confirmation-required result and writes an audit event.
3. The model summarizes the intended action and key arguments to the employee.
4. The employee confirms.
5. The model calls the same tool again with `confirmed=true`.

Audit logs are intended for operational review and incident reconstruction. They
record the tool reference, risk, decision, confirmation flag, reason, and
redacted arguments. They are not a replacement for downstream system logs or
business approval records.

This keeps the business integration layer in MCP while giving the runtime a
small, local approval and audit surface.

## Production Baseline

Use the GA tag for deployments:

```bash
git checkout enterprise-wecom-v0.0.8-ga
```

The detailed freeze record lives in
`examples/enterprise_wecom_digital_employee/PRODUCTION_FREEZE.md`.
