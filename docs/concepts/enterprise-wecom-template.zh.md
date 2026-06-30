---
title: 企业微信模板
type: explanation
audience: [A2, A4]
runs: no
verified_on: 2026-06-30
sources:
  - templates/deepagents/enterprise-wecom/README.md
  - templates/deepagents/enterprise-wecom/{{cookiecutter.project_slug}}/.env.example
  - contrib/agentseek-enterprise/src/agentseek_enterprise/mcp_policy.py
  - examples/enterprise_wecom_digital_employee/PRODUCTION_FREEZE.md
  - examples/enterprise_wecom_digital_employee/DEPLOYMENT_NOTES.md
---

# 企业微信模板

`deepagents/enterprise-wecom` 模板围绕 AgentSeek gateway、DeepAgents、员工身份、
MCP 工具和多层记忆，生成一个企业微信数字员工项目。

## 当前状态

`enterprise-wecom-v0.0.5-ga-20260630` 是当前 GA 基线。

它已经完成两类验证：

1. 仓库内 `examples/enterprise_wecom_digital_employee` 部署。
2. 通过 `agentseek create deepagents/enterprise-wecom` 渲染出的独立项目。

两个维度都通过了同一套企业微信 live smoke test：身份、短期记忆、显式长期记忆、
语义长期记忆、MCP 工具、sidecar 稳定性和企微重试去重。

## Runtime 形态

```text
企业微信智能机器人
-> agentseek-wecom channel
-> bub gateway
-> agentseek-enterprise 员工身份
-> agentseek-langchain RunnableSpec
-> DeepAgents agent
-> MCP tools
```

生成项目通过 `.agentseek/lifecycle.toml` 和 `scripts/run_gateway.sh` 管理自己的
runtime 细节。

## 记忆分层

| 层级 | 存储 | 用途 |
| --- | --- | --- |
| 短期记忆 | SQLAlchemy URL；本地 fallback 为 SQLite | 最近的 per-session 对话上下文 |
| 显式长期记忆 | 员工级 LangGraph Store；可由 SQLAlchemy URL 驱动，本地 fallback 为 SQLite | 员工明确要求助手长期记住的事实 |
| 语义长期记忆 | ContextSeek + SeekDB | 历史对话的语义召回 |

这三层是刻意分开的。短期记忆服务最近追问；显式长期记忆通过 memory tools 控制；
语义长期记忆自动检索相关历史上下文，不要求 agent 主动选择某个文件或笔记。

生产环境可以把前两层迁到 PostgreSQL/MySQL：
`AGENTSEEK_ENTERPRISE_MEMORY_SQLALCHEMY_URL` 控制短期记忆，
`AGENTSEEK_ENTERPRISE_STORE_SQLALCHEMY_URL` 控制显式长期记忆。语义长期记忆仍由
ContextSeek 的 backend 配置控制，例如本地 SeekDB 或后续的向量数据库适配器。

## 隔离选择

模板避免给 DeepAgents 使用共享宿主文件系统 backend。它只把可信部署指令和
skills 暴露为只读虚拟文件系统，并把持久记忆映射到员工级 store。

DM 身份查询可以使用 `subprocess` 或 `sidecar` 模式。两种模式都会把
JPype/libjvm 隔离在 gateway 主进程之外，使 ContextSeek SeekDB 和 ONNX 可以在
gateway 主进程中运行。

## MCP 策略和审计

MCP tools 是预订会议室、提交出差申请、数据查询等业务动作的边界。生成项目里的
`call_mcp_tool` adapter 会先评估本地策略，再调用远端 MCP server。

策略会区分 read/query 工具和 write/risky 工具。查询工具默认可以执行；写入或高风险
工具可以要求员工明确确认；denylist 命中的工具会在远端调用之前被阻断。每次 adapter
决策都可以写入脱敏后的 JSONL 审计日志。

这样业务集成仍然留在 MCP 层，但 runtime 拥有一个很小、可本地配置的审批和审计面。

## 生产基线

生产部署使用 GA tag：

```bash
git checkout enterprise-wecom-v0.0.5-ga-20260630
```

详细冻结记录在 `examples/enterprise_wecom_digital_employee/PRODUCTION_FREEZE.md`。
