---
title: 模板
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

# 模板

## 可用模板

| 模板 | 描述 |
| --- | --- |
| `bub/contextseek` | 带 ContextSeek 语义记忆和 AgentSeek 生命周期规范的 Bub agent。 |
| `bub/default` | 带 AgentSeek 生命周期规范的轻量 Bub agent。 |
| `deepagents/content-builder` | 带写作流程、图像生成、本地 UI 和 AgentSeek 生命周期规范的 DeepAgents 内容构建器。 |
| `deepagents/default` | 带 AgentSeek 生命周期规范的最小 DeepAgents 应用。 |
| `deepagents/enterprise-wecom` | 企业微信数字员工，包含员工身份、MCP 工具、pgvector 语义记忆和 AgentSeek 生命周期规范。 |
| `deepagents/research` | 带检索流程、本地 UI 和 AgentSeek 生命周期规范的 DeepAgents research 应用。 |
| `langchain/agentic-rag` | 带 OceanBase vector search 和 AgentSeek 生命周期规范的 LangChain agentic RAG。 |
| `langchain/agentic-rag-openvino` | 带本地 OpenVINO models 和 AgentSeek 生命周期规范的 LangChain agentic RAG。 |
| `langchain/cli-remote` | 把本地生命周期工作流连接到远程 LangGraph 服务的 LangChain 模板。 |
| `langchain/default` | 带本地 Web UI 和 AgentSeek 生命周期规范的 LangChain agent 应用。 |
| `langchain/markdown-messages` | 带 Markdown 消息渲染和 AgentSeek 生命周期规范的 LangChain chat 应用。 |
| `langchain/sandbox` | 带本地 UI 和 AgentSeek 生命周期规范的 sandbox coding agent。 |

## 模板 spec

| 形式 | 示例 |
| --- | --- |
| Type | `bub` |
| Type and name | `bub/default` |
| Absolute local path | `/path/to/template` |
| Git URL | `https://github.com/example/templates.git` |

## 选择和发现

| 命令 | 结果 |
| --- | --- |
| `agentseek create` | 交互式选择类型和模板。 |
| `agentseek create --list-templates` | 列出所有已知模板。 |
| `agentseek create bub --list-templates` | 只列出 `bub` 模板。 |
| `agentseek create bub` | 解析到 `bub/default`。 |
| `agentseek create bub/default` | 使用指定模板。 |
| `agentseek create bub --template default` | 使用 `bub/default`。 |
| `agentseek create --template` | 列出模板的兼容入口。新脚本优先使用 `--list-templates`。 |

## 企业微信模板

| 字段 | 值 |
| --- | --- |
| 模板 | `deepagents/enterprise-wecom` |
| Runtime | DeepAgents，经由 `agentseek-langchain` 和 `bub gateway` 运行 |
| 通道 | 通过 `agentseek-wecom` 接入企业微信智能机器人回调 |
| 身份 | 通过 `agentseek-enterprise` 注入员工身份上下文 |
| 业务工具 | 从 `.agents/mcp.json` 加载 MCP servers |
| MCP 策略 | 本地 allowlist/denylist、写入/高风险工具确认、JSONL 审计 |
| 短期记忆 | 按 session 隔离的 SQLAlchemy 记忆，SQLite fallback |
| 显式长期记忆 | 员工级 LangGraph Store memory tools，支持 SQLAlchemy 或 SQLite fallback |
| 语义记忆 | 生产使用 ContextSeek + PostgreSQL + pgvector；本地开发可回退 SeekDB |
| 生产检查 | 生成项目中的 `scripts/prod_check.py --env-file .env` |

创建形式：

```bash
agentseek create deepagents/enterprise-wecom
```

生成项目包含 `.agentseek/lifecycle.toml`、`scripts/run_gateway.sh`、
`scripts/bub_gateway.py`、`scripts/prod_check.py`、macOS LaunchAgent 模板，
以及用于放置 DM JDBC driver 的 `vendor/dameng/` 目录。

企业微信 MCP policy 配置项：

| 配置项 | 默认值 | 含义 |
| --- | --- | --- |
| `AGENTSEEK_ENTERPRISE_MCP_POLICY_ENABLED` | `true` | 在模板 adapter 中启用本地 MCP 策略检查。 |
| `AGENTSEEK_ENTERPRISE_MCP_DEFAULT_ACTION` | `allow` | 没有被 allowlist/denylist 拦截时的默认动作。 |
| `AGENTSEEK_ENTERPRISE_MCP_ALLOWLIST` | 空 | 可执行工具的 `server/tool` 模式列表，逗号分隔。 |
| `AGENTSEEK_ENTERPRISE_MCP_DENYLIST` | 空 | 在远端 MCP 调用前直接阻断的工具模式列表。 |
| `AGENTSEEK_ENTERPRISE_MCP_WRITE_TOOLS` | 空 | 标记为会改变企业状态的工具模式列表。 |
| `AGENTSEEK_ENTERPRISE_MCP_RISKY_TOOLS` | 空 | 标记为高风险操作的工具模式列表。 |
| `AGENTSEEK_ENTERPRISE_MCP_CONFIRM_TOOLS` | 空 | 即使是 `read` 也要求确认的工具模式列表。 |
| `AGENTSEEK_ENTERPRISE_MCP_REQUIRE_CONFIRMATION` | `true` | 对 `write`、`risky` 和 confirm-listed 工具要求显式确认。 |
| `AGENTSEEK_ENTERPRISE_MCP_AUDIT_ENABLED` | `true` | 将 MCP 决策事件写入 JSONL。 |
| `AGENTSEEK_ENTERPRISE_MCP_AUDIT_LOG_PATH` | `./runtime/mcp-audit.jsonl` | 审计 JSONL 路径；相对路径基于项目根目录。 |

Runtime 和记忆分层设计见[企业微信模板](../concepts/enterprise-wecom-template.md)。
