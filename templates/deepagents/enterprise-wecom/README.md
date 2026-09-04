# Enterprise WeCom DeepAgent Template

This template scaffolds a WeCom-facing enterprise digital employee:

- AgentSeek gateway runtime;
- `agentseek-wecom` callback channel;
- `agentseek-enterprise` employee identity injection;
- DeepAgents agent bound through `agentseek-langchain`;
- MCP business tools loaded from `.agents/mcp.json`;
- MCP policy and audit around the generated `call_mcp_tool` adapter;
- production semantic memory through ContextSeek + PostgreSQL + pgvector with
  bge-m3 ONNX embeddings;
- versioned digital-employee Profile references plus a read-only Strategic
  Development Department knowledge MCP simulator with keyword, semantic, and
  hybrid retrieval;
- inbound WeCom AI Bot file/image/video/mixed media intake through
  `agentseek-files`, with AES decrypt, HMAC-scoped storage, and
  `[CurrentFiles]` prompt context.
- an executable WeCom outbound capability probe that keeps callback-mode
  Artifact delivery fail-closed until a signed HTTPS download endpoint exists.

## Deployment Model

One generated project is one logical deployment unit for one digital employee.
The digital employee has one business identity (`digital_employee_id`), one
active Profile and capability pool, and may own zero or more Playbooks. A
Playbook is a workflow owned by that employee; it is not a separate employee or
deployment by itself.

One employee may serve many people and group chats. A department may also own
several digital employees, but each employee should be generated and deployed
as a separate logical unit when its role, owner, authorization, capability pool,
or audit boundary differs. Do not load several digital employees or Profiles
into one generated runtime.

For each WeCom AI Bot, choose exactly one inbound mode: Callback or long
connection. A self-built WeCom application can supplement the Bot with targeted
proactive delivery. If several employees share one application for outbound
delivery, keep the source employee, visibility, idempotency, and audit context
explicit; application inbound callbacks require an external router before they
can be shared safely.

Keep runtime files, durable messaging, short-term memory, semantic memory, Work
state, files, and audit data isolated per digital employee. In particular, the
current direct-message session key is compatible with `wecom:<userid>` and does
not itself contain `digital_employee_id`, so separate database/schema/table
prefixes or storage paths remain part of the deployment boundary.

The complete v0.1.2 reference diagram is available as a
[scalable SVG](../../../docs/assets/enterprise-wecom/enterprise-wecom-v0.1.2-architecture.svg)
or a [4096 × 2880 PNG](../../../docs/assets/enterprise-wecom/enterprise-wecom-v0.1.2-architecture-4k.png).
Read the [architecture and deployment boundaries](../../../docs/concepts/enterprise-wecom-architecture.md)
before creating a production employee deployment.

## Inputs

| Variable | Description |
| --- | --- |
| `project_name` | Human-readable project name. |
| `project_slug` | Python package and directory name. |
| `author` | Project author. |
| `default_model` | Default `AGENTSEEK_MODEL` value (`deepseek-v4-flash-0731`). |
| `wecom_port` | Local WeCom callback server port. |
| `wecom_callback_path` | Callback path configured in the WeCom intelligent robot. |
| `mcp_config_path` | MCP config path read by AgentSeek and the DeepAgents MCP adapter. |
| `deployment_path` | Absolute path used in the generated macOS LaunchAgent template. |
| `_agentseek_source_path` | Optional local editable AgentSeek source checkout. |
| `_agentseek_source_url` | Git source used when `_agentseek_source_path` is empty. |

## Generated Layout

```text
{{ cookiecutter.project_slug }}/
  .agents/mcp.json
  .agents/mcp.department-knowledge.example.json
  .agentseek/lifecycle.toml
  .env.example
  AGENTS.md
  README.md
  pyproject.toml
  launchd/
    com.local.{{ cookiecutter.project_slug }}.plist
  scripts/
    import_department_knowledge.py
    probe_department_knowledge.py
    probe_wecom_outbound.py
    prod_check.py
    run_gateway.sh
  skills/
    enterprise-employee/SKILL.md
    office-workflow/SKILL.md
  src/{{ cookiecutter.project_slug }}/
    __init__.py
    agent.py
    settings.py
    tools.py
    department_knowledge/
  vendor/dameng/
```

The generated project is intentionally backend-first. It is meant to be run by
`scripts/run_gateway.sh`, which loads the project `.env`, installs the DM JDBC
bridge extras, and starts `bub gateway --enable-channel wecom` through a small
Logfire-safe wrapper. The same process is declared in `.agentseek/lifecycle.toml`,
so `agentseek dev` can also start it under the AgentSeek lifecycle toolkit.
Directly invoking `bub_gateway.py` is unsupported because it bypasses the generated
project `PYTHONPATH`, dotenv loading, and runtime extras established by the script.
