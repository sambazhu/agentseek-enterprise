---
title: 企业数字员工框架研发指南
type: explanation
audience: [A1, A2, A3]
runs: no
verified_on: 2026-09-04
sources:
  - ../../README.md
  - ../../contrib/README.md
  - ../../templates/deepagents/enterprise-wecom/README.md
  - ROADMAP.md
  - ARCHITECTURE_AND_DEPLOYMENT_BOUNDARIES.md
  - V0.1.2_M0_PLATFORM_FREEZE.md
  - src/enterprise_wecom_digital_employee/agent.py
  - src/enterprise_wecom_digital_employee/capability_registry.py
  - src/enterprise_wecom_digital_employee/playbook_registry.py
  - digital_employees/industry-report/profile.yaml
  - digital_employees/industry-report/pack.yaml
---

# 企业数字员工框架研发指南

本文面向负责开发、部署和维护企业数字员工的研发人员，解释框架的职责边界、
核心概念和扩展方式。当前可部署基线为 `enterprise-wecom-v0.1.2-ga`。

配套文档：

- [当前基线、限制与后续路线](ROADMAP.md)
- [数字员工架构、概念与部署边界](ARCHITECTURE_AND_DEPLOYMENT_BOUNDARIES.md)
- [原平台边界与第二部门冻结（v0.1.3 输入）](V0.1.2_M0_PLATFORM_FREEZE.md)
- [快速部署与创建项目](DEVELOPER_QUICKSTART.md)
- [扩展 Skill、MCP 和 Playbook](EXTENDING_DIGITAL_EMPLOYEE.md)
- [研发分支、验证与合并流程](DEVELOPMENT_WORKFLOW.md)

## 框架解决什么问题

这套框架不是一个只会聊天的机器人，也不是把所有业务逻辑写进一段系统提示。
它把企业数字员工拆成可部署、可授权、可审计和可演进的几个部分：

1. 通过企微接收员工消息和文件，并解析经过认证的员工身份。
2. 为一名部门数字员工声明岗位、职责、服务目录和行为准则。
3. 将文件分析、部门知识和 MCP 数据能力组成统一能力池。
4. 对需要正式交付的工作使用 Playbook，保留任务、版本、审批和审计记录。
5. 通过确定性路由、服务端状态机和输出守卫约束模型的不确定性。
6. 用模板生成新项目，并用生命周期命令完成检查、启动和生产预检。

## 总体架构

```text
员工 / 企业微信
      |
      v
agentseek-wecom
  加密回调、消息队列、背压、文件接收、卡片和下载链接
      |
      v
agentseek-enterprise
  租户、员工身份、短期记忆、持久记忆、策略、审计和可观测性
      |
      v
部门数字员工运行时
  Job Charter / Profile
  确定性服务发现与 Playbook 路由
  Unified Capability Registry
      |                              |
      | 普通协助                     | 正式服务
      v                              v
DirectTurn                       Playbook Binding
Skill / 文件 / 知识 / MCP         同一能力池 + WorkItem + 合同 + 状态机
      |                              |
      +---------------+--------------+
                      v
              DeepAgents / LangChain
                      |
                      v
       agentseek-work / 文件与业务账本 / Artifact
```

### 各层职责

| 层 | 负责 | 不负责 |
| --- | --- | --- |
| AgentSeek | 项目模板、生命周期规范、CLI 和集成发行 | 代替业务 Playbook |
| Bub | Gateway、Channel、Plugin 和 Hook 生命周期 | 定义企业岗位 |
| DeepAgents / LangChain | 模型推理、工具调用和消息编排 | 作为业务账本真相源 |
| contrib 插件 | 企微、身份、文件、记忆、Work、调度等可复用运行时能力 | 某个部门的专属流程 |
| Profile / Pack | 数字员工身份、能力上限、服务目录和不可变版本 | 执行具体业务状态迁移 |
| Capability Registry | 向普通协助和 Playbook 提供同一组业务能力 | 允许模型任意拼 MCP server/tool |
| Playbook | 正式任务、合同、检查点、审批、发布和交付 | 承担所有普通问答 |

## 一个逻辑部署单元对应一名数字员工

当前采用以下产品模型：

```text
一个逻辑部署单元
  -> 一名数字员工
  -> 一个 Job Charter / Profile
  -> 一个统一能力池
  -> 零个或多个正式 Playbook

一个 AI Bot（Callback 或长连接二选一）
  -> 作为这名数字员工的主要企微入口

可选公共自建应用
  -> 作为主动通知和文件投递的补充出口
```

同一名数字员工可以逐步增加同一岗位边界内的服务。例如战略发展部数字员工可以
在证券行业正式报告之外增加属于战略发展岗位的正式服务。岗位、业务 Owner、
组织授权或知识边界不同，就应新建另一个 Bot 和数字员工，不能为了展示
Multi-Playbook 而混合部门。

这里的“一名数字员工”不是一名使用企微的人类员工，也不是一个 Playbook。
它可以同时服务多名员工、多个群聊和多个同岗位边界内的 Playbook。一个部门也可以
部署多名职责不同的数字员工；每名数字员工使用独立部署单元。

当前一个部署单元由一个进程运行。未来同一逻辑数字员工可以增加多个高可用副本，
但这些副本仍共享同一个 `digital_employee_id` 和一致性边界。完整定义和数据隔离
要求见 `ARCHITECTURE_AND_DEPLOYMENT_BOUNDARIES.md`。

v0.1.3 计划的第二个真实部署采用独立的信息技术部 Bot、Profile 和 Pack，服务为
“信息系统需求评审与立项评估”。它与战略发展部数字员工复用同一套 SDK 和
Capability 接口，但不共享部门身份、任务账本或审批边界。同 Bot Multi-Playbook
继续由测试 fixture 验证。

当前 `digital_employee_id: industry-report` 是已经写入 WorkItem、PackSnapshot 和
事件的技术标识。展示名称可以升级为“战略发展部数字员工”，但不要为改名而直接
修改这个历史技术 ID。

## Job Charter 与 Profile

`digital_employees/industry-report/profile.yaml` 是数字员工的岗位章程和权限上限，
包括：

- 员工编号、展示名称、所属部门、岗位角色和使命；
- 岗位职责、正式服务目录、员工示例请求；
- 面向员工的行为准则和服务边界；
- 可使用的 Skill、知识、工具授权、数据范围和策略；
- 可以加载的 Playbook；
- 请求人范围和异常升级策略。

员工问“你是谁”“你能做什么”“怎么使用你”时，框架从这里确定性生成介绍，
不依赖模型临场发挥。员工问“我是谁”时，则读取认证后的人类员工身份，不能把
数字员工身份和人类身份混在一起。

Profile 是权限天花板，不是“写上就一定可用”。有效能力是以下条件的交集：

```text
Profile 声明
  ∩ 当前部署真实配置
  ∩ 员工授权范围
  ∩ 企业策略
```

Playbook 只能申请 Profile 已允许的 Skill、工具和数据范围，不能自行扩大权限。

## 统一能力池

Skill、文件能力、部门知识和 MCP 不再分成“普通对话专用”和“正式服务专用”
两套。`CapabilityRegistry` 只建立一个 Profile 所属的能力池，然后同时注入：

- DirectTurn：普通问答、资料查询、文件分析等协助；
- Playbook：正式任务中的研究、分析或数据调用。

区别不在于能力来源，而在于是否需要正式治理：

- 普通协助可以调用同一个 Skill 或 MCP，但不能静默创建 WorkItem。
- 正式 Playbook 调用知识或外部数据时，要把来源、员工决策和结果摘要登记到
  任务账本。
- 正式工作中的外部研究必须走 Playbook 的版本化缺口决策，不能绕过为一次
  普通 MCP 调用。

对模型暴露的是稳定的业务工具，例如 `search_department_knowledge` 和
`search_public_information`。底层固定映射到真实 MCP server/tool。不要重新暴露
通用 `call_mcp_tool(server_name, tool_name)`，否则模型可能混淆名称或尝试未授权
工具。

## Skill、MCP、Playbook 和 Plugin

| 需求 | 应选扩展点 | 判断标准 |
| --- | --- | --- |
| 增加方法、规则、术语或操作指导 | Skill | 主要是可复用知识和步骤 |
| 接入知识库、数据平台或业务系统 | MCP + Capability Registry | 需要访问外部系统或执行动作 |
| 增加正式、可追踪的部门服务 | Playbook | 有多阶段任务、版本、人工检查点或审批 |
| 增加跨项目复用的运行时集成 | contrib Plugin | 需要 Bub Hook、Channel 或基础设施适配 |
| 改岗位、职责或能力上限 | Profile | 改变“是谁、能做什么、谁能用” |
| 让新创建的项目默认包含改动 | enterprise-wecom Template | 改动应进入所有后续脚手架 |

一个能力可以先服务普通协助，之后再被 Playbook 使用，不需要复制一套实现。

## Playbook 与普通协助

路由不是先让 LLM 猜。当前顺序是：

1. 已存在的 WorkItem 和精确合同动作；
2. 员工明确选择某个正式服务；
3. 确定性关键词唯一匹配；
4. 无法唯一匹配时，列出真实服务让员工澄清；
5. 不属于正式服务时，进入普通 DirectTurn。

普通对话可以使用 Skill、文件和已授权 MCP。Playbook 只在正式服务被选中时增加：

- `WorkItem`：一次持续的正式工作；
- 版本化合同：Brief、Outline、Draft、Approval 等；
- 状态机：只允许合法状态迁移；
- 人工检查点：确认、提交审批、批准、发布、交付彼此分离；
- 证据、事件和幂等记录；
- 输出守卫：模型不能声称账本里不存在的业务事实。

当前 `delivered` WorkItem 仍占用同一员工、数字员工和 Playbook 的活跃范围，这是
为了让后续修订继续追加在同一个 WorkItem 上，而不是创建第二份互相冲突的账本。

## 当前证券行业报告服务

证券行业报告 Playbook 的完整链路是：

```text
员工提出正式报告需求
  -> WorkItem
  -> ReportBrief：主题、覆盖期、范围、格式
  -> 内部研究：模板问题、SourceRecord、覆盖度和缺口
  -> 缺口决策：继续留缺口、上传材料、授权专业数据或公开搜索
  -> ReportOutline：依据当前研究结果形成提纲
  -> Evidence + Claim + ReportDraft：形成可审阅初稿
  -> ReportApproval：内容审批
  -> ReportArtifact：内容寻址 DOCX
  -> ReportPublication：登记正式发布版本
  -> ReportDelivery：一次性短时签名链接交付
```

研究模板和 Outline 不重复：

- 研究模板定义“必须调查哪些问题”，用于检索、覆盖度和缺口判断。
- Outline 定义“这一次报告如何组织”，只纳入适用于当前主题且有证据或明确缺口的内容。
- Brief 是本次需求合同；模板是研究检查表；Outline 是本次成文结构。

例如员工要写“利率变化对证券行业的影响”：

- Brief 固定主题、时间范围和读者；
- 模板要求检查行业趋势、经营指标、业务线影响和风险；
- 内部知识先回答模板问题，政策时点无内部材料则登记缺口；
- 员工授权公开搜索后补齐政策证据；
- Outline 再把结果组织为执行摘要、影响机制、业务线分析和建议，而不是照抄模板。

## Pack 与不可变快照

`pack.yaml` 把 Profile、Skill、Policy、Asset、Playbook 和评测清单组成一个可验证
发行单元。启动正式工作时，框架登记内容寻址 `PackSnapshot`，历史任务始终可以
解释“当时使用了哪一版规则和模板”。

因此：

- 修改 Pack 内任何内容后必须升级 `pack_version`；
- 修改 Profile 语义时同时升级 `profile_version`；
- 修改 Skill 内容时升级 Skill 版本、更新 SHA256 和所有引用；
- 不要覆盖旧 PackSnapshot，也不要删除历史合同来“解锁”开发；
- 正式状态变化只能走服务端工具和状态机。

## 当前 contrib 插件

本示例组合了以下可复用包。详细配置以各包自己的 README 为准：

- [`agentseek-wecom`](../../contrib/agentseek-wecom/README.md)：企微 AI Bot Channel、
  消息队列、文件回调、卡片和下载链接。
- [`agentseek-enterprise`](../../contrib/agentseek-enterprise/README.md)：身份、租户、
  记忆、MCP 策略、审计和企业事件。
- [`agentseek-langchain`](../../contrib/agentseek-langchain/README.md)：将 LangChain /
  DeepAgents Runnable 绑定到 Bub turn。
- [`agentseek-files`](../../contrib/agentseek-files/README.md)：员工文件的隔离存储和解析。
- [`agentseek-contextseek`](../../contrib/agentseek-contextseek/README.md)：语义记忆检索。
- [`agentseek-work`](../../contrib/agentseek-work/README.md)：WorkItem、合同、Artifact、
  PackSnapshot、Repository 和状态机。
- [`agentseek-schedule-sqlalchemy`](../../contrib/agentseek-schedule-sqlalchemy/README.md)：
  持久化调度。

业务 Playbook 不应做成 contrib Plugin；只有跨项目复用的运行时集成才放入
`contrib/`，并通过 `[project.entry-points.bub]` 注册。

## enterprise-wecom Template

`templates/deepagents/enterprise-wecom` 是新建企业数字员工项目的脚手架，生成：

- 生命周期规范、环境变量样例和生产预检；
- 企微 Gateway 与 launchd 入口；
- 企业身份、记忆、文件和 MCP 配置；
- 部门知识库 MCP 示例及导入脚本；
- Profile、Pack、Skill、Policy、Asset 和报告 Playbook；
- 运行时目录。

示例目录是已经持续验证的参考实现。凡是希望后续新项目自动获得的能力，都要
同步修改 Cookiecutter 模板，并运行模板渲染测试；只改示例不会自动更新脚手架。
当前模板尚未生成与示例工程等价的 Profile、权限、路由和 Playbook 单测骨架，
该项已进入 v0.1.3 M2。

## 安全和治理底线

研发改动必须保持以下边界：

- 不提交 `.env`、`mcp.local.json`、数据库密码、模型文件、JDBC 凭据或下载 token。
- 不在模型回复、事件或日志中暴露 OA 明文、内部路径、`storage_key`、`grant_hash`。
- 不让模型自行声明已确认、已批准、已发布或已交付；服务端账本是真相源。
- 外部数据和公开搜索按员工最新消息的明确授权执行。
- Playbook 权限必须是 Profile 权限子集。
- 不直接 `UPDATE`/`DELETE` 正式 Work、合同、证据、Artifact 或 Delivery。
- 生产交付当前使用一次性短时签名链接；浏览器 SSO 尚未纳入 v0.1.2 GA。

## 当前平台限制

在 v0.1.3 完成对应平台化切片前，部署和扩展时必须接受以下边界：

- 跨部门授权仍有展示名称匹配遗留；第二个部门服务必须等稳定组织 ID 和成员关系
  校验完成后再上线生产。
- 会话队列默认是一条 active turn 加三条 pending；只有已接纳消息承诺
  exactly-once，超出容量的 burst 会明确拒绝，不进入 Agent。
- 队列和 durable inbox/outbox 已支持单实例重启恢复；多主机共享
  存储、同会话单飞和实例失效接管尚未完成。
- signed-link 下载只有短时、一次性、recipient-bound token，没有浏览器侧
  OIDC、SAML 或企微网页身份二次校验。
- `delivered` 任务仍占用同一 requester、数字员工和 Playbook 的活跃范围；
  正式归档和另建同类任务尚未实现。
- Evidence/Claim 绑定提供可追溯性，不等于独立事实审查；当前正式产物只支持 DOCX。

完整业务边界和 v0.1.3 实施顺序以
[Roadmap](ROADMAP.md) 为准；原 M0 冻结文档保留为范围转移记录。

下一步请根据目标选择：

- 首次部署或创建项目：[快速部署与创建项目](DEVELOPER_QUICKSTART.md)
- 新增能力或正式服务：[扩展 Skill、MCP 和 Playbook](EXTENDING_DIGITAL_EMPLOYEE.md)
- 准备提交代码：[研发分支、验证与合并流程](DEVELOPMENT_WORKFLOW.md)
