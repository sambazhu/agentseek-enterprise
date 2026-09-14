# agentseek-skill-mcp

## At A Glance

| Item | Value |
| --- | --- |
| Distribution | `agentseek-skill-mcp` |
| Python package | `agentseek_skill_mcp` |
| Bub entry point | `skill_mcp` |
| Workspace path | `contrib/agentseek-skill-mcp` |
| Test target | `make test-skill-mcp` |
| Type check target | `make typecheck-skill-mcp` |

## When To Use It

Use this plugin to connect an AgentSeek agent to an **Agent Skill & MCP Platform**
and dynamically pull three kinds of configuration at runtime:

- **Skills** — platform-authored skill definitions are exported to local files so
  the DeepAgent virtual filesystem can serve them as read-only context.
- **MCP configs** — remote MCP server definitions are written to `.agents/mcp.json`
  instead of being loaded as `BaseTool` instances.
- **Model configs** — the platform can override or provide the model configuration
  used by the agent.

This plugin is designed for the `enterprise-wecom` template and its
`call_mcp_tool` adapter. It deliberately does **not** register MCP tools as
LangChain `BaseTool`. Writing to `.agents/mcp.json` preserves the enterprise
`call_mcp_tool` policy / audit / confirmation chain, so every remote MCP call
still flows through the local MCP policy gateway.

## Install

In this monorepo, install the plugin group:

```bash
uv sync --group plugins
```

## Configure

All configuration is via `AGENTSEEK_SKILL_MCP_*` environment variables:

| Variable | Default | Description |
| --- | --- | --- |
| `AGENTSEEK_SKILL_MCP_ENABLED` | `false` | Master switch for the plugin. When `false`, no platform calls are made. |
| `AGENTSEEK_SKILL_MCP_BASE_URL` | _empty_ | Base URL of the Agent Skill & MCP Platform (e.g. `https://skill-mcp.internal.corp`). |
| `AGENTSEEK_SKILL_MCP_API_KEY` | _empty_ | API key used to authenticate against the platform. Sent as a bearer token. |
| `AGENTSEEK_SKILL_MCP_AGENT_ID` | _empty_ | Identifier of the agent on the platform whose skills / MCP / model config should be loaded. |
| `AGENTSEEK_SKILL_MCP_OUTPUT_DIR` | `.agents` | Directory where `mcp.json` and exported skill files are written. |
| `AGENTSEEK_SKILL_MCP_TIMEOUT_SECONDS` | `30` | HTTP timeout for platform requests. |
| `AGENTSEEK_SKILL_MCP_SYNC_ON_STARTUP` | `true` | Whether to sync skills and MCP config once when the agent is built. |

Minimal example:

```env
AGENTSEEK_SKILL_MCP_ENABLED=true
AGENTSEEK_SKILL_MCP_BASE_URL=https://skill-mcp.internal.corp
AGENTSEEK_SKILL_MCP_API_KEY=sk-xxxxxxxxxxxxxxxx
AGENTSEEK_SKILL_MCP_AGENT_ID=wecom-digital-employee
```

## Run

The plugin hooks into the agent build lifecycle. When
`AGENTSEEK_SKILL_MCP_ENABLED=true`, `sync_skills()` and `sync_mcp_config()` are
called during `build_agent()`, pulling the latest definitions from the platform
and writing them to the local filesystem before the agent starts serving turns.

## Runtime Behavior

| Step | What happens |
| --- | --- |
| `load_agent_config()` | Fetches the agent definition (skills, MCP servers, model config) from the platform for `AGENTSEEK_SKILL_MCP_AGENT_ID`. |
| `sync_skills()` | Exports each skill to a file under `AGENTSEEK_SKILL_MCP_OUTPUT_DIR` so the DeepAgent virtual filesystem can serve them. |
| `sync_mcp_config()` | Writes remote MCP server definitions to `.agents/mcp.json`. The enterprise `call_mcp_tool` adapter reads this file to open MCP clients on demand. |
| `resolve_model_config()` | Returns the model configuration from the platform, which can override template defaults. |
| `build_skill_system_prompt()` | Assembles a system-prompt fragment listing available skills for the agent. |

**Important:** MCP tools are **not** loaded as `BaseTool`. Because the config is
written to `.agents/mcp.json`, every remote MCP call passes through the
enterprise `call_mcp_tool` adapter and therefore through the MCP policy engine
(denylist / allowlist / risk classification), the audit JSONL writer, and the
confirmation flow. This keeps the security chain intact regardless of where the
tool definitions originate.

## Verify

```bash
make test-skill-mcp
make typecheck-skill-mcp
```

To confirm the platform is reachable and credentials are valid:

```bash
uv run python -c "from agentseek_skill_mcp import get_platform_client; print(get_platform_client().health())"
```

## Limitations

- The plugin currently treats MCP config as file-based output (`.agents/mcp.json`).
  It does not open MCP client connections itself; that remains the responsibility
  of the `call_mcp_tool` adapter.
- Skills are exported as static files. If a skill changes on the platform, the
  agent must rebuild (or `AGENTSEEK_SKILL_MCP_SYNC_ON_STARTUP` must trigger) to
  pick up the update.
- There is no offline cache. If the platform is unreachable during startup,
  `sync_skills()` / `sync_mcp_config()` will fail fast and the agent will start
  without platform-provided configuration.
