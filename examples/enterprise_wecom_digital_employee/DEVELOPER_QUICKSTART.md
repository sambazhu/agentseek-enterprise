---
title: 快速部署与创建企业数字员工
type: tutorial
audience: [A1, A2]
runs: yes
verified_on: 2026-09-02
sources:
  - ../../docs/guides/create-project.zh.md
  - ../../templates/deepagents/enterprise-wecom/cookiecutter.json
  - .agentseek/lifecycle.toml
  - .env.example
  - scripts/run_gateway.sh
  - scripts/prod_check.py
---

# 快速部署与创建企业数字员工

本教程提供两条起步路径：

- 部署当前已验证的部门数字员工：使用 `production` 或
  `enterprise-wecom-v0.1.2-ga`。
- 创建一个新的部门数字员工项目：使用 `deepagents/enterprise-wecom` 模板。

完成后，你将得到可由 `agentseek info`、`doctor`、`task` 和 `dev` 管理的项目。

## 前置条件

- Python 3.12 或 3.13；
- `uv`；
- 可访问的模型服务；
- 企微 AI Bot 的 Token、EncodingAESKey 和回调配置；
- 员工身份数据源；
- 正式 Work 模式所需的 PostgreSQL；
- 使用语义记忆或部门知识时所需的 `pgvector` 和 bge-m3 ONNX 文件。

凭据和内网地址只写入部署机 `.env` 或密钥管理系统，不写入 Git。

## 路径一：部署已验证 GA

内部网络可从公司 GitLab 拉取。下面命令需要真实仓库访问权限，本次文档验证未
执行网络克隆；已用当前工作区同一 GA 源码完成了后续 CLI 和模板验证。

```bash title="not executed in this run"
git clone -b production \
  http://172.200.6.12:9091/harness_agent/agentseek-enterprise.git
cd agentseek-enterprise
git switch --detach enterprise-wecom-v0.1.2-ga
```

公司网外使用必须同步的 GitHub 回退源：
`https://github.com/sambazhu/agentseek-enterprise.git`。所有发布分支、
`production` 和正式 tag 都必须在 GitLab/GitHub 保持同一 commit。

开发环境可以留在 `production`，生产部署和回滚建议固定到 GA tag。

安装依赖：

```bash title="not executed in this run"
uv sync --all-packages --all-extras --group plugins
```

该命令会解析或下载依赖，本次文档验证使用了已同步的工作区环境。

进入示例：

```bash title="not executed in this run"
cd examples/enterprise_wecom_digital_employee
cp .env.example .env
```

本次未覆盖工作区已有 `.env`；相同复制步骤已在隔离模板目录验证。

## 路径二：从模板创建新项目

安装 AgentSeek CLI 后，在一个不会与现有目录重名的位置运行：

```bash title="not executed in this run"
agentseek create deepagents/enterprise-wecom
```

CLI 会询问项目名称、包名、作者、默认模型、企微端口、回调路径和部署路径。
企业模板当前默认模型为 `deepseek-v4-flash-0731`；部署环境可在本机
`.env` 中显式覆盖，不应提交凭据或环境专用值。
交互式输入未在本次自动化文档验证中执行。
自动化脚本可以使用默认值：

```bash
agentseek create deepagents/enterprise-wecom --no-input
cd enterprise_wecom_digital_employee
```

模板的本地渲染已在隔离临时目录验证，生成项目包含：

```text
enterprise_wecom_digital_employee/
  .agents/mcp.json
  .agentseek/lifecycle.toml
  .env.example
  digital_employees/industry-report/
  scripts/
  src/enterprise_wecom_digital_employee/
  tests/
```

如果你在 AgentSeek 源码仓库中开发模板，可直接用本地路径验证：

```bash
agentseek create ./templates/deepagents/enterprise-wecom --no-input
```

## 配置环境

复制环境变量样例：

```bash title="not executed in this run"
cp .env.example .env
```

部署机已有 `.env` 时不得运行该命令覆盖它；本次只在隔离模板目录验证。

至少按以下分组配置：

1. 模型：`AGENTSEEK_MODEL_PROVIDER`、`AGENTSEEK_MODEL`、API key/base。
2. 企微：回调路径、Token、EncodingAESKey、Corp ID、App Secret。
3. 身份：DM/JDBC 或你实现的企业身份 Provider。
4. 企业隔离：tenant、namespace secret、员工记忆存储。
5. Work 账本：`AGENTSEEK_WORK_ENABLED=true`、SQLAlchemy URL、快照和 Artifact 目录。
6. ContextSeek：开发可用本地后端；生产建议 PostgreSQL + pgvector。
7. 文件：大小、扩展名、解析器和 MinerU 配置。
8. MCP：配置路径、策略、审计日志和具体 server。
9. 交付：公开 HTTPS base URL、一次性签名授权和响应模式。

生成高强度 namespace secret：

```bash title="not executed in this run"
scripts/prod_check.py --generate-namespace-secret
```

该命令会生成真实 secret，本次未执行。不要把输出提交到仓库。

## 配置 MCP

仓库提交的 `.agents/mcp.json` 应只包含可公开的结构，不含凭据。本机差异写入被
Git 忽略的 `.agents/mcp.local.json`，并把 `AGENTSEEK_MCP_CONFIG_PATH` 指向它。

部门知识示例位于：

```text
.agents/mcp.department-knowledge.example.json
```

将需要的 server 合并到本机 MCP 配置，不要覆盖已有的 Gildata 或 Tavily 配置。
配置名称必须与 Capability Registry 的固定映射一致。

## 检查生命周期项目

以下命令已在模板生成项目中实际执行：

```bash
agentseek info
agentseek doctor
```

`info` 应显示：

```text
Template: deepagents/enterprise-wecom
Dev: agentseek dev
```

在尚未创建 `.env` 或未填凭据时，`doctor` 会明确列出缺项。这是预期行为。

配置完成后执行严格检查：

```bash title="not executed in this run"
agentseek doctor --strict
agentseek task prod-check
```

这两个生产检查需要已填写的真实 `.env`，本次用缺省模板验证了其缺项报告。

也可以直接运行：

```bash title="not executed in this run"
scripts/prod_check.py --env-file .env
```

`prod_check` 只输出脱敏后的存在性、路径、权限和生产开关检查，不应回显密钥。

## 启动 Gateway

支持的入口是：

```bash title="not executed in this run"
scripts/run_gateway.sh
```

也可以通过生命周期工具启动：

```bash title="not executed in this run"
agentseek dev
```

启动命令需要真实模型、企微、身份和数据库配置，本次未启动服务。

不要裸跑 `scripts/bub_gateway.py`。`run_gateway.sh` 会设置项目
`PYTHONPATH`、加载 `.env`、准备 JDBC 运行时，并用无缓冲模式启动 Gateway；
绕过脚本可能导致首个 Playbook turn 才出现包导入错误。

本地默认监听：

```text
http://127.0.0.1:12000/ai-bot/callback/demo/<botid>
```

正式环境还需要由运维配置可信 HTTPS 入口、企微回调域名和进程托管。

## 首次冒烟验证

先发送不会创建正式任务的消息：

```text
你是谁
你能做什么
怎么使用你
我是谁
查看当前报告任务状态
```

期望：

- 前三条由 Job Charter 确定性回答；
- “我是谁”返回认证后的人类员工身份；
- 状态查询读取服务端账本，不由模型编造；
- 未产生意外 WorkItem；
- 日志中没有 Traceback、凭据或 OA 明文。

再按当前服务目录执行一个完整正式流程。证券行业报告的最小路径为：

```text
创建报告需求
确认 ReportBrief vN
处理研究缺口
确认 ReportOutline vN
生成可审阅初稿
确认 ReportDraft vN
提交 ReportDraft vN 审批
批准 ReportDraft vN
生成 ReportDraft vN DOCX
发布 ReportArtifact vN
交付 ReportArtifact vN 给我
```

所有 `vN` 都必须使用机器人本轮返回的真实版本，不要照抄示例数字。

## 上线前检查

- `agentseek doctor --strict` 通过；
- `agentseek task prod-check` 通过；
- Profile、Pack、Skill SHA 和 PackSnapshot 注册无冲突；
- Work schema 已通过正式 migration 到当前 revision；
- 企微身份、文件、部门知识、MCP 和交付链路分别验证；
- 外部数据调用在无员工明确请求时 fail-closed；
- 下载链接一次性、短时且无原始路径泄漏；
- `.env`、`mcp.local.json`、模型、runtime 和 token 均未进入 Git；
- 使用 GA tag 记录部署 commit，并保留可执行回滚方案。

接下来：

- 了解架构：[企业数字员工框架研发指南](DEVELOPER_GUIDE.md)
- 增加能力：[扩展 Skill、MCP 和 Playbook](EXTENDING_DIGITAL_EMPLOYEE.md)
- 提交改动：[研发分支、验证与合并流程](DEVELOPMENT_WORKFLOW.md)
