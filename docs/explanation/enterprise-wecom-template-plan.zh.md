---
title: Enterprise WeCom DeepAgents Template Plan
---

# Enterprise WeCom DeepAgents Template Plan

本记录描述 `deepagents/enterprise-wecom` 模板的第一版范围。该模板面向企业微信单聊数字员工，优先跑通 runtime 闭环，再逐步扩展长期记忆、权限治理和更多业务技能。

## 主要参考模板

1. `deepagents/default`
   - 作为主 runtime/gateway binding 基座。
   - 复用 `create_deep_agent(...) -> agentseek_langchain.messages_spec(...) -> agentseek gateway` 路径。
2. `deepagents/research`
   - 参考后端结构：`agent.py`、`prompts.py`、`tools.py`。
   - 适合知识问答、资料查询和综合分析类企业场景。
3. `langchain/default`
   - 参考 `.env.example`、`settings.py`、部署说明和企业聊天通道文档风格。
4. `deepagents/content-builder`
   - 第一版采用它的 `AGENTS.md`、`skills/`、`subagents.yaml` 生态组织方式。
   - 不复制内容生产、图片生成和复杂前端产品形态。
5. `bub/contextseek`
   - 作为后续语义记忆层参考，不作为第一版主基座。

## 第一版模板布局

```text
templates/deepagents/enterprise-wecom/
  cookiecutter.json
  README.md
  {{cookiecutter.project_slug}}/
    .env.example
    .gitignore
    AGENTS.md
    README.md
    pyproject.toml
    subagents.yaml
    skills/
      knowledge-qa/SKILL.md
      office-workflow/SKILL.md
    src/{{cookiecutter.project_slug}}/
      __init__.py
      agent.py
      gateway_binding.py
      identity.py
      mcp_tools.py
      memory.py
      prompts.py
      settings.py
      tools.py
```

## 关键功能

1. 企业微信单聊入口
   - 通过 `agentseek-wecom` 接入自建应用或智能机器人回调。
   - 支持加密验签、文本消息、stream 响应和欢迎事件。

2. 员工身份上下文
   - 通过 `agentseek-enterprise` 从 runtime state 读取 `employee_context`。
   - 将员工姓名、OA 账号、组织类型、部门/营业部/子公司、岗位等信息注入 agent 上下文。

3. DeepAgents 数字员工主体
   - 使用 `create_deep_agent(...)` 构建主 agent。
   - 第一版就保留 `AGENTS.md`、`skills/`、`subagents.yaml`，方便后续扩展业务技能。

4. MCP 工具接入
   - 第一版读取现有 MCP 配置。
   - 会议室预定、出差申请、知识问答等业务实现放在 MCP tools，不写死到 runtime。

5. 短期记忆持久化
   - 第一版使用 SQLite 保存单个员工会话。
   - 推荐 session id 形态：`wecom:<userid>`。
   - 保存最近 N 轮对话和摘要，重启后恢复上下文。

6. 二次确认机制
   - 查询类操作可以直接执行。
   - 提交、变更、审批类操作必须先复述关键字段并等待用户确认。

7. 联调脚本
   - 内置本地企微加密回调探针。
   - 验证 `employee_context` 注入、agent 输出和 stream 响应。

## 第一版闭环

```text
企微用户发消息
-> agentseek-wecom 接收并解密
-> gateway 创建会话
-> agentseek-enterprise 根据 userid 查询员工身份
-> 注入 employee_context
-> 读取该员工短期记忆
-> DeepAgent 理解问题
-> 必要时调用 MCP tools
-> 生成回答
-> 写入短期记忆
-> 通过企微 stream 返回
```

## 第一版验收目标

1. 正式企微回调能进入 gateway。
2. 能识别单个员工身份。
3. agent 回答能感知员工组织信息。
4. 同一员工多轮对话能持久化。
5. MCP 工具能被 agent 调用。
6. 提交类业务会先二次确认。
7. 有本地 smoke test 可以快速定位通道、身份、模型或 MCP 问题。

## 已知前置修复

1. `agentseek-schedule-sqlalchemy` 在没有 URL 时应 fail-open，避免中断普通 turn。
2. `agentseek-enterprise` 的 JDBC 依赖应整理成 optional extra，例如 `agentseek-enterprise[jdbc]`。
