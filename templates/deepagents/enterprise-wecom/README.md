# Enterprise WeCom DeepAgent Template

This template scaffolds a WeCom-facing enterprise digital employee:

- AgentSeek gateway runtime;
- `agentseek-wecom` callback channel;
- `agentseek-enterprise` employee identity injection;
- DeepAgents agent bound through `agentseek-langchain`;
- MCP business tools loaded from `.agents/mcp.json`;
- MCP policy and audit around the generated `call_mcp_tool` adapter.

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
| `deployment_path` | Absolute path used in the generated macOS LaunchAgent template. |
| `_agentseek_source_path` | Optional local editable AgentSeek source checkout. |
| `_agentseek_source_url` | Git source used when `_agentseek_source_path` is empty. |

## Generated Layout

```text
{{ cookiecutter.project_slug }}/
  .agents/mcp.json
  .agentseek/lifecycle.toml
  .env.example
  AGENTS.md
  README.md
  pyproject.toml
  launchd/
    com.local.{{ cookiecutter.project_slug }}.plist
  scripts/
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
  vendor/dameng/
```

The generated project is intentionally backend-first. It is meant to be run by
`scripts/run_gateway.sh`, which loads the project `.env`, installs the DM JDBC
bridge extras, and starts `bub gateway --enable-channel wecom` through a small
Logfire-safe wrapper. The same process is declared in `.agentseek/lifecycle.toml`,
so `agentseek dev` can also start it under the AgentSeek lifecycle toolkit.
