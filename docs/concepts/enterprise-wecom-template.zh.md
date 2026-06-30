---
title: 企业微信模板
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

# 企业微信模板

`deepagents/enterprise-wecom` 模板围绕 AgentSeek gateway、DeepAgents、员工身份、
MCP 工具和多层记忆，生成一个企业微信数字员工项目。

## 当前状态

`enterprise-wecom-v0.0.4-ga-20260629` 是第一版 GA 基线。

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
| 短期记忆 | SQLite | 最近的 per-session 对话上下文 |
| 显式长期记忆 | SQLiteStore | 员工明确要求助手长期记住的事实 |
| 语义长期记忆 | ContextSeek + SeekDB | 历史对话的语义召回 |

这三层是刻意分开的。短期记忆服务最近追问；显式长期记忆通过 memory tools 控制；
语义长期记忆自动检索相关历史上下文，不要求 agent 主动选择某个文件或笔记。

## 隔离选择

模板避免给 DeepAgents 使用共享宿主文件系统 backend。它只把可信部署指令和
skills 暴露为只读虚拟文件系统，并把持久记忆映射到员工级 store。

DM 身份查询可以使用 `subprocess` 或 `sidecar` 模式。两种模式都会把
JPype/libjvm 隔离在 gateway 主进程之外，使 ContextSeek SeekDB 和 ONNX 可以在
gateway 主进程中运行。

## 生产基线

生产部署使用 GA tag：

```bash
git checkout enterprise-wecom-v0.0.4-ga-20260629
```

详细冻结记录在 `examples/enterprise_wecom_digital_employee/PRODUCTION_FREEZE.md`。

