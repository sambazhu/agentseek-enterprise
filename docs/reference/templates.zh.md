---
title: 模板
type: reference
audience: [A1, A2]
runs: no
verified_on: 2026-06-30
sources:
  - templates/index.json
  - src/agentseek/cli/commands/create.py
  - templates/deepagents/enterprise-wecom/README.md
  - templates/deepagents/enterprise-wecom/{{cookiecutter.project_slug}}/.env.example
---

# 模板

## 可用模板

| 模板 | 描述 |
| --- | --- |
| `bub/contextseek` | 带 ContextSeek 语义记忆和 AgentSeek 生命周期规范的 Bub agent。 |
| `bub/default` | 带 AgentSeek 生命周期规范的轻量 Bub agent。 |
| `deepagents/content-builder` | 带写作流程、图像生成、本地 UI 和 AgentSeek 生命周期规范的 DeepAgents 内容构建器。 |
| `deepagents/default` | 带 AgentSeek 生命周期规范的最小 DeepAgents 应用。 |
| `deepagents/enterprise-wecom` | 企业微信数字员工，包含员工身份、MCP 工具、语义记忆和 AgentSeek 生命周期规范。 |
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
| 短期记忆 | 按 session 隔离的 SQLite 记忆 |
| 显式长期记忆 | 员工级 SQLiteStore memory tools |
| 语义记忆 | 默认使用 ContextSeek + SeekDB |
| 生产检查 | 生成项目中的 `scripts/prod_check.py --env-file .env` |

创建形式：

```bash
agentseek create deepagents/enterprise-wecom
```

生成项目包含 `.agentseek/lifecycle.toml`、`scripts/run_gateway.sh`、
`scripts/bub_gateway.py`、`scripts/prod_check.py`、macOS LaunchAgent 模板，
以及用于放置 DM JDBC driver 的 `vendor/dameng/` 目录。

Runtime 和记忆分层设计见[企业微信模板](../concepts/enterprise-wecom-template.md)。
