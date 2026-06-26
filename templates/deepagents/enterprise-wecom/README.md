# Enterprise WeCom DeepAgent Template

This template scaffolds a WeCom-facing enterprise digital employee:

- AgentSeek gateway runtime;
- `agentseek-wecom` callback channel;
- `agentseek-enterprise` employee identity injection;
- DeepAgents agent bound through `agentseek-langchain`;
- MCP business tools loaded from `.agents/mcp.json`.

## Inputs

| Variable | Description |
| --- | --- |
| `project_name` | Human-readable project name. |
| `project_slug` | Python package and directory name. |
| `author` | Project author. |
| `default_model` | Default `AGENTSEEK_MODEL` value. |
| `wecom_port` | Local WeCom callback server port. |
| `wecom_callback_path` | Callback path configured in the WeCom intelligent robot. |
| `mcp_config_path` | MCP config path read by AgentSeek and the DeepAgents MCP adapter. |
| `_agentseek_source_path` | Optional local editable AgentSeek source checkout. |
| `_agentseek_source_url` | Git source used when `_agentseek_source_path` is empty. |

## Generated Layout

```text
{{ cookiecutter.project_slug }}/
  .agents/mcp.json
  .env.example
  AGENTS.md
  README.md
  pyproject.toml
  skills/
    enterprise-employee/SKILL.md
    office-workflow/SKILL.md
  src/{{ cookiecutter.project_slug }}/
    __init__.py
    agent.py
    settings.py
    tools.py
```

The generated project is intentionally backend-first. It is meant to be run by `agentseek gateway --enable-channel wecom`.
