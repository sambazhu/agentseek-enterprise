---
title: Enterprise WeCom Evolution Roadmap
type: explanation
audience: [A2, A3, A4]
runs: no
verified_on: 2026-08-03
sources:
  - examples/enterprise_wecom_digital_employee/README.md
  - examples/enterprise_wecom_digital_employee/PRODUCTION_FREEZE.md
  - examples/enterprise_wecom_digital_employee/DEVELOPER_GUIDE.md
  - examples/enterprise_wecom_digital_employee/V0.1.0_INDUSTRY_REPORT_DIGITAL_EMPLOYEE_PLAN.md
  - examples/enterprise_wecom_digital_employee/V0.1.0_M0_FREEZE.md
  - examples/enterprise_wecom_digital_employee/V0.1.1_DEPARTMENT_DIGITAL_EMPLOYEE_PLAN.md
  - examples/enterprise_wecom_digital_employee/V0.1.1_M4_IMPLEMENTATION.md
  - examples/enterprise_wecom_digital_employee/V0.1.2_M0_PLATFORM_FREEZE.md
  - examples/enterprise_wecom_digital_employee/digital_employees/industry-report/profile.yaml
  - examples/enterprise_wecom_digital_employee/digital_employees/industry-report/pack.yaml
  - docs/concepts/enterprise-wecom-template.zh.md
  - templates/deepagents/enterprise-wecom/README.md
---

# Enterprise WeCom 演进路线

本文记录 `enterprise-wecom` 的已交付基线、当前限制和后续路线。

文档中的 v0.0.9、v0.1.0 和 v0.1.1 章节保留历史决策与验收记录。
从 v0.1.2 开始的章节才是当前待执行路线，不能把历史章节中的早期设想
当作尚未实现的需求。

## 当前定位

`enterprise-wecom` 当前已经是企业数字员工的 runtime harness、项目脚手架和
受治理任务执行样例。

它已经完成的生产底座包括：

- 企业微信智能机器人接入；
- 员工身份解析和组织、岗位上下文注入；
- 多员工隔离；
- 短期记忆；
- 显式长期记忆；
- PostgreSQL + pgvector 语义记忆；
- 企业 MCP 工具接入；
- MCP policy 和 audit；
- Langfuse 与本地 JSONL 双通道观测；
- PostgreSQL SCRAM、最小权限账号、pgvector、bge-m3 ONNX 等生产化配置；
- Mac mini 单实例准生产验证。

v0.1.0 和 v0.1.1 在此基础上进一步完成：

- Profile、Job Charter、服务目录和统一 Capability Registry；
- 确定性 Playbook 路由、歧义澄清和当前任务续接；
- WorkItem、版本化合同、人工确认点、审批和审计；
- ReportBrief、内部知识优先研究、缺口决策和外部检索授权；
- ReportOutline、Evidence、Claim、ReportDraft 和内容审批；
- DOCX Artifact、发布账本、一次性 signed-link 交付和下载消费；
- 单会话串行化、队列背压、快速 ACK、exactly-once 交付和健康回归。

当前生产基线是 `enterprise-wecom-v0.1.1-ga`。产品形态仍是：

```text
一个企微 Bot
-> 一名部门数字员工
-> 一个 Profile 级共享能力池
-> 一个已上线的证券行业报告 Playbook
```

Multi-Playbook Registry 已经实现，但第二个 Playbook 仍是测试夹具，尚未完成
第二个真实部门数字员工与业务服务的生产验证。v0.1.2 M0 已将该目标
冻结为信息技术部数字员工的“信息系统需求评审与立项评估”。

所以 v0.0.8 之后的 `enterprise-wecom`，已经不只是一个聊天示例。
它是可部署、可观测、可审计的企业数字员工运行底座。

当前最强的路径是：

```text
员工发消息
-> 识别员工身份
-> 发现部门数字员工服务并确定性选择 Playbook
-> 建立 WorkItem 和版本化业务合同
-> 优先检索部门知识，按员工授权补充外部数据
-> 形成 Evidence、Claim、提纲和初稿
-> 经过人工确认、内容审批、渲染和发布
-> 生成一次性 signed-link 并登记交付
-> 全程执行 policy、audit、观测和脱敏
```

这说明它已经具备一条完整的企业任务完成链路，而不只是 MCP 调用能力。

当前主要限制不再是“能不能生成报告”，而是平台复用与生产规模化：

- 部门授权仍依赖特定 Scope 和组织名称匹配，尚未形成通用组织授权模型；
- 身份 Provider 当前以 DM 为主，缺少标准的可插拔 Provider Registry；
- Capability Registry 已统一，但新增业务 MCP 仍需要编写 Python 适配代码；
- 只有一个真实生产 Playbook，通用边界尚未经过第二项业务服务验证；
- 企微队列、stream 和去重仍以单进程内存态为主，不支持多实例协同和重启恢复；
- signed-link 使用短时一次性 Token，尚未增加浏览器侧 OIDC、SAML 或企微网页身份认证；
- Report Claim 具备来源绑定和可追溯性，但尚未形成独立事实核验闭环；
- `delivered` WorkItem 用于持续修订，会长期占用同类任务 Scope，尚无正式归档和新建机制；
- 报告 Composition 与 Output Guard 模块偏大，继续增加合同类型会提高维护成本；
- 模板能生成项目，但尚未随模板提供完整的扩展测试骨架和第二 Playbook 示例。

因此下一阶段不是重复建设 research、content 或 DOCX，而是把已经验证的
证券报告实现提炼成可复用、可扩展、可规模化运行的平台能力。

## 关键澄清

### Research 模板为什么单独存在

`deepagents/research` 单独作为模板，不是因为其他模板不能做 research。
它的价值是提供一个最小、纯粹、易学习的 research agent 样板。

它适合：

- 学习 research loop；
- 验证搜索工具；
- 排查 DeepAgents research 行为；
- 给其他模板复制和参考。

`enterprise-wecom` 完全可以支持 research。
但它应该实现企业版 research workflow，而不是把 `deepagents/research`
整个模板硬合并进去。

企业版 research 会额外依赖：

- 员工身份；
- 组织和岗位上下文；
- 企业 MCP 工具；
- 上传文件；
- 企业合规边界；
- 企微回复和文件交付。

### Content-builder 如何融入

`content-builder` 表达的是内容生产工作流：

```text
research -> plan -> draft -> revise -> export
```

它可以融入 `enterprise-wecom`，但不应整体搬入。
更合适的方式是把它的能力拆成 template 内的工作流模块：

```text
content/
  planner.py
  writer.py
  reviewer.py
  exporter.py
  schemas.py
```

这些模块服务于企业内部场景，例如：

- 写汇报材料；
- 写制度解读；
- 写培训稿；
- 写会议纪要；
- 写 PPT 大纲；
- 根据受众调整表达；
- 做合规审查。

### Files 应该是插件，不是新的 runtime 架构

AgentSeek 本身已经是插件化生态。
文件能力不应该引入一套新的 File Runtime 架构。

更合适的是新增：

```text
contrib/agentseek-files
```

它作为通用文件插件，负责：

- 接收文件流；
- 文件隔离存储；
- 文件解析；
- 文件上下文构建；
- 输出文件登记；
- 为后续文件发送预留接口。

Channel 插件负责接入具体平台。
例如 `agentseek-wecom` 知道如何从企微智能机器人回调里读取短期有效的
media URL、下载并解密文件，以及发送文件。
`agentseek-files` 不关心文件来自企微、飞书还是 Web。

### DeepResearch 第一版放在 template

DeepResearch 第一版不应先抽象成通用 contrib 插件。
它更像 `enterprise-wecom` 模板里的企业工作流能力。

建议先放在：

```text
templates/deepagents/enterprise-wecom/{{cookiecutter.project_slug}}/
  src/{{cookiecutter.package_name}}/research/
```

成熟之后，再评估是否沉淀为：

```text
contrib/agentseek-deepresearch
```

### MCP、Files、Research、Content 的关系

可以用这组职责来理解：

```text
Enterprise-WeCom = 企业数字员工的身体和工作环境
MCP              = 手和工具
Files            = 输入材料和输出文件的承载层
Research         = 找资料、查证、综合判断
Content          = 组织表达、写作、生成产物
```

工程结构上可以演进为：

```text
enterprise-wecom runtime
  ├─ identity / memory / audit / observability
  ├─ MCP tools: 企业业务动作和数据查询
  ├─ agentseek-files: 文件输入输出插件
  ├─ research workflow: 多步信息收集和综合
  └─ content workflow: 写作、改稿、生成材料
```

MCP 提供工具能力。
Research 和 Content 提供任务组织能力。
Files 提供材料输入和交付物输出。

## 为什么只丰富 MCP 不够

如果没有 research 和 content，能力扩展主要依赖增加 MCP tools。

这能提升“能调用哪些系统”，但不能显著提升“如何完成复杂任务”。

例如员工提出：

```text
帮我准备一份面向分支机构的 AI 工具培训材料
```

这不是一个单一 MCP 工具可以完成的任务。
它需要：

```text
理解目标
-> 规划结构
-> 收集资料
-> 调用 MCP / 搜索 / 附件
-> 组织表达
-> 合规审查
-> 生成初稿
-> 多轮修改
-> 导出文档
```

因此后续能力扩展应分成两条线：

```text
1. MCP 增加可调用能力
2. Workflow 增加任务组织能力
```

这两条线组合后，`enterprise-wecom` 才能从工具调用助手升级为任务完成型数字员工。

## v0.0.9：agentseek-files 插件

v0.0.9 已在以下分支启动：

```text
enterprise/v0.0.9-files-plugin
```

该版本的目标是新增：

```text
contrib/agentseek-files
```

本轮开发采用该分支作为起点。当前分支已经加入：

- `contrib/agentseek-files` 文件插件；
- `agentseek-wecom` 对 `file`、`image`、`voice` 回调的媒体下载接线；
- HMAC scope 的本地文件存储；
- 本地文本提取器；
- MinerU remote extractor 的提交/轮询接口；
- `[CurrentFiles]` state 注入和 `enterprise-wecom` prompt 注入；
- example/template 依赖、preflight 和 README 配置说明。

仍需继续补齐：

- 真企微文件消息 live 验证；
- MinerU 真实 API live 验证；
- 慢解析完成后的独立主动通知持久 poller；
- 文件观测事件和 Langfuse 脱敏专项验收；
- 生成文件回传能力。

### 目标闭环

员工在企微发送文件后，系统可以：

1. 识别文件消息；
2. 下载文件；
3. 按租户、员工、日期、会话隔离存储；
4. 提取元数据；
5. 对支持的文件类型做基础解析或远程解析；
6. 把当前 turn 文件摘要注入 agent state；
7. 记录脱敏观测事件；
8. 模型可以基于当前附件回答问题。

### 推荐目录结构

```text
runtime/files/
  <tenant_key>/
    <employee_key>/
      <yyyy-mm-dd>/
        <session_key>/
          inbound/
            <file_id>/
              original
              metadata.json
              extracted.txt
          outbound/
            <file_id>/
              generated.docx
              metadata.json
```

这里的 `tenant_key`、`employee_key`、`session_key` 都应使用 HMAC。
目录中不应出现明文 OA 账号、明文会话 id 或员工姓名。

### 文件元数据

建议使用统一的 `FileRecord`：

```json
{
  "file_id": "file_...",
  "direction": "inbound",
  "tenant_key": "hmac-...",
  "employee_key": "hmac-...",
  "session_id": "wecom:hmac-...",
  "channel": "wecom",
  "chat_id": "hmac-...",
  "message_id": "hmac-...",
  "filename": "example.pdf",
  "mime_type": "application/pdf",
  "size_bytes": 123456,
  "sha256": "...",
  "created_at": "2026-07-08T...",
  "expires_at": "2026-07-15T..."
}
```

首版可以只实现 `direction=inbound`。
但数据模型应预留 `outbound`，方便后续生成文件并发给用户。

### 首版支持文件类型和解析方式

v0.0.9 建议把解析能力分成两层。

本地轻量 extractor 先支持：

- `.txt`
- `.md`
- `.csv`
- `.json`

这些类型可以在 gateway 进程内解析，不需要外部命令、脚本执行或沙箱。

远程 extractor 可以通过 MinerU 支持：

- `.pdf`
- `.docx`
- `.pptx`
- `.xlsx`
- 图片文件，如 `.png`、`.jpg`、`.jpeg`

MinerU 应作为 `agentseek-files` 的可插拔 remote extractor，而不是写进
`agentseek-wecom` 或 `enterprise-wecom` 模板。

首版建议支持两种 MinerU 模式：

```text
mineru_agent   ：Agent 轻量解析 API，单文件，10MB，20 页，仅 Markdown
mineru_precise ：精准解析 API，Token，200MB，200 页，Markdown/JSON/Zip
```

生产环境优先使用 Token 驱动的精准解析 API；轻量 API 适合试用或小文件。

v0.0.9 不做本地复杂转换。
任何需要外部命令、脚本执行、LibreOffice、Pandoc 或本地复杂转换的处理，
都应进入 v0.1.0 受 WorkItem 授权的任务级 sandbox 后再启用。

首版暂不做：

- OCR；
- 本地复杂 Word/PPT 解析；
- 本地图片理解；
- 文件长期语义入库；
- 多文件异步任务；
- 自动生成 docx/pdf/pptx；
- DeepResearch 完整闭环。

### MinerU 异步解析和主动通知

MinerU 是异步解析服务。v0.0.9 的用户体验应区分三类情况：

```text
快速解析完成
-> 当前会话直接回复“文件已解析完成”，并可给出摘要

短轮询窗口内未完成
-> 当前会话回复“文件已收到，正在解析，完成后我会通知你”

后台解析完成
-> 主动通知用户“你刚才上传的文件已解析完成，可以继续提问”
```

`FileRecord` 需要记录解析状态和通知状态：

```json
{
  "extract_status": "pending|running|done|failed",
  "extract_provider": "mineru",
  "extract_task_id": "...",
  "notify_on_done": true,
  "notify_channel": "wecom",
  "notified_at": null
}
```

首版不引入 Celery / Redis。单实例 Mac mini 可以先使用轻量 poller：

```text
FilesExtractionPoller
  每 N 秒扫描 pending/running 文件
  查询 MinerU 状态
  下载结果
  更新 FileRecord
  如果 notify_on_done=true 且 notified_at 为空，则通知用户
```

通知能力不应由 `agentseek-files` 直接绑定企业微信实现。
`agentseek-files` 只产生 `file_extract_done` / `file_extract_failed`
这类事件或回调，具体发送由 channel adapter 或 template 接线完成。

通知必须防重复：成功通知后写入 `notified_at`。

### 插件分工

`agentseek-wecom` 负责：

- 识别企业微信智能机器人 `file`、`image`、`video`、`voice`、`mixed`
  消息；
- 从 `file.url`、`image.url`、`video.url` 读取 5 分钟有效的下载 URL；
- 立即下载加密文件；
- 使用回调 `EncodingAESKey` 按 AES-256-CBC 解密；
- 直接使用 `voice.content` 作为语音转写文本；
- 把解密后的文件流交给 `agentseek-files`。

`agentseek-files` 负责：

- 文件存储；
- 文件名清洗；
- 大小限制；
- MIME / 扩展名校验；
- HMAC scope；
- 文本提取；
- MinerU remote extractor；
- 解析状态持久化；
- 解析完成事件；
- 上下文构建；
- 文件观测事件。

`enterprise-wecom` template 负责：

- 启用 files 插件；
- 把 `state["current_files"]` 注入 prompt；
- 支持当前附件问答和总结；
- 预留 research/content 使用文件的接口。

### 安全边界

v0.0.9 必须默认做到：

- 最大文件大小限制；
- 扩展名 allowlist；
- MIME sniffing；
- 路径归一化；
- 员工和会话隔离；
- SHA256 去重；
- Langfuse 不记录文件正文；
- 本地 JSONL 不记录文件正文；
- 不向模型暴露宿主机真实路径；
- 不向 Langfuse 暴露文件正文、MinerU Token、企微签名下载 URL；
- MinerU Token 只从环境变量读取；
- 解析失败时优雅降级。

### v0.0.9 验收标准

- 企业微信上传 `.txt` 后，当前 turn 可以总结文件内容；
- 企业微信上传 PDF 后，可以走 MinerU 提交解析任务；
- MinerU 快速完成时，当前会话能基于解析结果回答；
- MinerU 慢解析时，系统能记录 pending 状态，并在完成后主动通知用户；
- 另一个员工无法访问该文件；
- `runtime/files` 路径中没有明文 OA、员工姓名或明文会话 id；
- Langfuse 和本地 JSONL 不包含文件正文、MinerU Token 或 OSS 签名 URL；
- 超大文件和非 allowlist 文件被拒绝；
- MCP policy/audit 行为不变。

## v0.1.0：行业报告编写数字员工

v0.1.0 的首个企业岗位场景确定为“行业报告编写数字员工”。

它服务公司战略发展部，负责证券行业发展报告和公司专题报告的材料整理、
研究分析、提纲评审、报告编写、质量检查、版本修订和正式交付。

v0.1.0 的产品中心不再是 sandbox。它是企业工作任务运行层：

```text
数字员工岗位
-> 持久 WorkItem
-> 报告 Playbook
-> Source / Evidence / Claim
-> 人工评审和批准
-> 版本化报告产物
-> 任务事件和审计
```

详细规划见 `V0.1.0_INDUSTRY_REPORT_DIGITAL_EMPLOYEE_PLAN.md`。

### 核心交付

- 新增通用 `contrib/agentseek-work` Bub 插件，承载 WorkItem、事件、审批和 worker；
- 行业报告数字员工岗位档案；
- 版本化 industry-report DigitalEmployeePack；
- Profile-scoped Skills、Playbooks、tool grants、assets、policies 和 evals；
- 报告任务合同 `ReportBrief`；
- 可跨重启恢复的 `WorkItem` 和 `WorkEvent`；
- `securities_industry_report/v1` 标准作业流程；
- 来源、证据和关键主张之间的可追溯关系；
- 按来源许可保存快照或稳定复核记录，并显式处理 EvidenceConflict；
- 提纲评审、最终批准和版本绑定；
- WorkItem/WorkEvent 恢复真相源、WorkBudget 和 `waiting_external`；
- 外部模型、MinerU、MCP、消息和文件交付前的 egress 硬策略；
- `python-docx` 和战略发展部批准模板生成的 DOCX，可选 PDF；
- outbound 文件登记和企微交付；
- `work_id` 贯穿业务事件、Langfuse 和 MCP audit；
- 简化组织映射：战略发展部委派人兼任评审人、批准人、数据所有者和交付对象；
- 信息技术部 Agent 运维负责运行保障，不默认读取业务内容；
- 权限、保密等级、人工接管和任务运营入口。

### M0 冻结门

冻结状态、证据和签定结果统一登记在 `V0.1.0_M0_FREEZE.md`。

- 短连接 `response_url` 一次性延迟回复探针；
- 公司 Word 模板或获批的临时中性模板；
- 来源快照、商业许可和保留策略；
- 企微稳定 `section_id` 结构化评审意见；
- DirectTurn/WorkItem 路由规则和 tool contract；
- 一个限定的证券行业报告验收主题。

### M1 工程合同

- 最小 `pack.yaml` schema、受限 `pack_loader.py` 和 Profile-scoped SkillResolver；
- 内容寻址 `PackSnapshot`，同时记录 source commit，保证历史 pack 可取回；
- WorkItem 绑定 `pack_snapshot_id` 和权威 `runtime_release`；
- 同一 tenant/requester/digital-employee/playbook 最多一个非终态 WorkItem，由 revision 5 partial unique index 保证；
- Langfuse runtime release 的 OTel/SDK 兼容探针，Langfuse 字段仅作辅助观测；
- pack 测试覆盖 manifest、skill_refs、版本固定、snapshot 取回、路径逃逸和二进制 asset 隔离。

industry-report pack 在 M4 建立 contract/model eval 基线，并在 M6 纳入 RC/GA 回归。
`smart-office` 是未来 cookiecutter 第一方可选 pack，不随 v0.1.0 发布，首版不拆独立仓库。

### M2 报告合同与知识优先研究

M2 按四个小切片推进，避免把澄清、检索、外部授权和写作一次性耦合：

1. **M2-01**：轻量、渐进式 `ReportBrief`；覆盖期可推断；默认交付 SLA 为 50 分钟；
   通用 work ledger 保存 provisional/confirmed/superseded 版本。
2. **M2-02**：Profile 增加 `knowledge_refs`；部门知识库经 MCP 接入；
   本地 PostgreSQL 以 keyword + pgvector hybrid 模拟相同合同。
3. **M2-03**：按批准的报告模板形成检索计划；先 list/glob，再 keyword/grep，
   再 semantic 补充，最后只读取入选片段并形成 SourceRecord 与 coverage。
4. **M2-04**：部门知识不足时向员工展示缺口，由员工选择 Gildata/公开搜索、
   上传材料或保留缺口继续生成。

当前状态：M2-01、M2-02、M2-03、M2-04 已通过 Mac mini PostgreSQL 与企微活体验证，
M2 正式关闭，可以进入 M3。
M2-03 已实现随 PackSnapshot 固定的模板检索 manifest、已确认 ReportBrief
守卫、内部知识编排、schema revision 7 SourceRecord 账本和确定性
coverage/gap。M2-04 已实现版本绑定的缺口选择、外部检索授权和
可审计的 Gildata/Tavily SourceRecord 登记。首次活体验证发现固定行业问题会
掩盖报告主题证据缺口，以及模型可绕过工具直接输出报告正文。修复版 Pack 1.2.0
新增 `report_topic` 直接证据问题，并在 parse-output 交付边界增加 M2 正文守卫和
审计事件。最终复验确认 coverage/选项/resolve 输出不会被误拦，Gildata/Tavily、
合同修订、幂等重放和 fail-closed 均通过。当前 Pack 的主题直接证据问题有意保持
严格；`choices=[]` 是合同级边界而不是当前样例知识库的 live 必达场景，不为制造
该场景降低关键词证据门槛。

M3 先以运行时前置切片启动：

1. **M3-00A 会话背压（已完成）**：同一 WeCom session 保持一个 active turn，默认最多三个
   pending；记录排队位置，超限不入 Agent，等待超过 TTL 自动结束；“查看消息队列”
   绕过模型即时返回。不同 session 继续并发。带 `response_url` 的 WeCom AI Bot 回调在
   Agent worker 运行前同步固化 `finish=true` ACK、排队位置或拒绝；进入 Agent 的消息再由
   `response_url` exactly-once 投递最终结果。Langfuse 网络与 flush 在有界 daemon 队列中
   异步发送，观测故障不能阻塞 callback；队列满时只丢 telemetry，不影响本地 JSONL 或业务。
   企业 harness 保留 DeepAgents 默认 subagent 作为可选执行助手；它不参与普通会话与
   WorkItem 的路由。隐式自动摘要保持关闭；插件层首模型回调 watchdog 直接约束
   `model_invoke` 到首个 provider callback 的 pre-model 长延迟，整轮 timeout 和
   model-node timeout 继续作为后备。Mac mini 已验证单聊、五消息 burst、队列背压、TTL、
   Langfuse 不可达和 response_url exactly-once 全部通过，M3-00A 正式关闭。
2. **M3-00B 控制与硬抢占**：取消当前处理、清空等待消息、阻塞 LLM 的硬超时和
   under-load SIGTERM 兜底。
3. **M3-00C Research Scope Contract（已完成）**：在报告提纲与正文生成前收口研究适用域。
   v0.1.0 将当前角色定位为“证券行业研究与报告数字员工”，而不是任意行业的通用
   报告生成器。允许证券行业、证券公司、证券业务线，以及外部因素对证券行业的影响；
   纯非证券主题在 ReportBrief 保存/确认前要求员工澄清，不得进入证券专属研究模板。

   - ReportBrief 增加可版本化的 `research_scope`，首版只接受
     `securities_industry` / `securities_company` / `securities_business_line` /
     `external_factor_on_securities`；范围与其他 Brief 字段一起由员工显式确认。
   - 不使用 embedding 或检索低分来自动授权研究域；服务端使用结构化范围、确定性规则和
     员工确认，以便审计与重放。
   - Pack/Playbook 显式声明版本化 `research_template_ref`，Loader 校验它属于已声明 Skill
     并纳入 PackSnapshot；运行时从已加载 Pack 解析，移除 `_RESEARCH_TEMPLATE` 的字面路径。
     `skill_refs` 继续承担完整性和能力声明，不再被误当作隐式模板路由。
   - 当前 `neutral-industry-report-internal-research` 不再声称中性通用；模板改为证券公司
     研究模板，将具体公司名改为可审计的组织上下文或“本公司”。
   - 计划编译阶段先判断问题适用性。整份 Brief 越界标记 `SCOPE_MISMATCH`；单个问题
     不适用标记 `NOT_APPLICABLE` 且不计入 coverage 分母；问题适用但证据不足才标记
     `GAP`；证据充足标记 `COVERED`。不得仅因为全部检索低分就把数据缺口判成主题错配。
   - 通用多行业能力留到后续 v0.1.x，通过多份经审核模板路由实现；不在生产运行时
     让 LLM 临时生成整套研究问题。

   当前实现基线已升级为 Pack `1.3.0` / Profile `1.2.0` / ReportBrief schema v2 / 研究模板
   schema v2。Mac mini 已验证证券正常路径、越界 fail-closed、外部因素 `3 applicable + 3
   not_applicable`、旧 schema v1 只读兼容、PackSnapshot 注册和 M3-00A burst，M3-00C 正式关闭。
4. **M3-01 ReportOutline 合同（已完成）**：从冻结研究模板和当前 SourceRecord 账本确定性生成
   `report-outline` 通用版本合同。章节使用稳定 `section_id`，每个适用研究问题绑定复数
   `source_ids`，未覆盖问题显式进入 `unresolved_question_ids`；不适用问题不进入提纲。

   - 提纲精确绑定当前 confirmed ReportBrief 版本、研究计划 digest、research scope、模板版本、
     来源集合 digest，以及必要时的 gap-decision contract 版本。
   - 内部研究存在缺口时，必须先完成当前版本的显式缺口决策。`upload_materials` 继续等待材料；
     Gildata/Tavily 必须已为每个缺口登记 SourceRecord；`continue_with_gaps` 可以形成提纲，但
     未解决问题必须原样保留。
   - 提纲先保存为 provisional；只有员工最新消息明确确认准确的 `ReportOutline vN` 才能确认。
     ReportBrief、gap decision 或来源集合变化后，旧提纲 fail-closed，必须重新生成版本。
   - 运行时守卫要求模型对提纲“已生成/保存/确认”的声明必须有本轮同版本
     `build/get/confirm_report_outline` 或 `get_current_work_status` 只读账本结果作为证据；只读状态
     仅能证明实际返回的版本和状态，不能把 provisional 叙述为 confirmed。
   - M3-01 只交付提纲合同，不生成报告正文、Markdown、DOCX、PDF、Evidence 或 Claim。

   Mac mini 已在 `4080d57` 验证证券提纲、外部因素裁剪、精确确认、账本真实性、幂等、M3-00A
   burst 和 MCP 零差异，M3-01 正式关闭。验证中发现的状态查询误拦已在进入 M3-02 前以窄白名单修复。
   确认引导也统一为可照抄的 `确认 ReportBrief vN` / `确认 ReportOutline vN`；
   严格版本门保持不变，不接受在 Brief 和 Outline 版本号可能相同时有歧义的 `确认 vN`。
   provisional 状态查询只信任 `get_current_work_status` 返回的同版本、同状态 Brief/Outline
   账本证据，claim 按合同所在行解析，不让相邻 Brief/Outline 的状态串扰；模型不自行
   校验确认语的大小写、空格或拼写，服务端 parser 保持唯一裁决。
5. **M3-02 Evidence-backed ReportDraft（已完成）**：
   基于 confirmed ReportOutline 和 SourceRecord 形成可审计的初稿、确定性质量门与 Markdown。

   - schema revision 8 新增不可变、tenant-scoped 的 EvidenceRecord、ClaimRecord 和
     Claim↔Evidence 关系表；幂等写入不改写旧证据或旧 Claim。
   - `prepare_report_draft_context` 只重读已被当前 Outline 选中的部门知识片段，
     核对 content hash，并将定位符、有界摘录、来源和适用章节固化为 Evidence。
     未保存 raw result 的外部 Source 不伪装成可写作证据。
   - `build_report_draft` 只接受结构化 Claim；事实与推断必须引用当前章节
     Evidence ID，风险/建议可以无引用但保持 `unverified`。模型不得用记忆或常识
     补齐 unresolved question。
   - 服务端确定性渲染 Markdown，检查 Outline 绑定、Claim/Evidence 绑定、
     locator、敏感路径/凭据特征和未解决缺口。ReportDraft 先保存为 provisional，
     它不等于语义事实已复核：所有模型 Claim 先保持 `unverified`，语义/人工审阅
     留给后续切片。
   - 交付边界只允许 `build_report_draft` / `get_current_report_draft` 返回的带标记
     Markdown，并丢弃模型二次改写；伪造 ReportDraft 版本/已生成声明仍 fail-closed。
   - 本切片不做 Draft 确认/审批、语义 Claim 核验、DOCX/PDF 渲染、成品交付。
     Pack/Profile 分别升级为 `1.4.0` / `1.3.0`，新增可快照化的 `report-writing@1.0.0` Skill。

   Mac mini 已在 `5c27033` 完成 schema rev8、Pack 1.4.0、Evidence/Claim 精确绑定、
   Markdown 逐字交付、幂等与负向边界活体复核，验证文档为 `a70b5b5`，
   M3-02 正式关闭。

   - 保留观察：DeepSeek 可能在员工确认 `continue_with_gaps` 后同轮调用 `build_report_outline`；只要
     服务端版本门和账本结果成立即可接受，不强制拆成两轮。
   - 保留观察：研究、缺口决策和提纲构建同轮执行时，外部观测查询可能遇到极短的事务可见性窗口；
     当前账本最终一致且工具链内读写正确，M3-02 增加阶段事件后再评估是否需要事务快照状态。
6. **M3-03 ReportDraft 确认与离散检查点（已完成）**：

   - `确认 ReportOutline vN` 只确认提纲，当轮不准备 Evidence、不保存 Claim、不生成 Draft；
     员工必须后续显式请求“生成可审阅初稿”才能进入写作。
   - provisional Draft 只有在员工最新消息精确确认 `ReportDraft vN` 时才进入
     confirmed；通用“确认”、错版本、否定、疑问或请求确认均 fail-closed。
   - Draft confirmed 只表示任务委派人认可当前 Markdown 审阅稿，不等于最终审批、
     发布或交付；本切片不引入 `approved` 状态，schema 保持 rev8。
   - Brief/Outline/Draft 的只读状态散文统一只信任服务端发布的 `current_work`
     账本快照；同版本同状态可放行，伪造 v999 或将 provisional 冒充 confirmed 仍拦截。
   - Pack/Profile 升级为 `1.5.0` / `1.4.0`，`report-writing` 升级为 `1.1.0`；
     example 和 cookiecutter 模板保持同步。

   Mac mini 已在 `ae34958` 完成离散检查点、Draft 精确确认、账本真实性和回归活体复核，
   验证文档为 `e0fbfe8`，M3-03 正式关闭。

7. **M3-04A ReportApproval 内容审批合同（已完成）**：

   - confirmed ReportDraft 只有在员工最新消息精确提交 `ReportDraft vN` 审批后，才建立
     独立、版本化的 `report-approval` provisional 合同；审批合同绑定 Draft 精确版本、
     canonical payload digest 和政策 ID。
   - 只有 WorkItem 账本中的 approver，且最新消息精确批准同一 `ReportDraft vN`，才能把
     当前审批合同推进为 confirmed（对外语义为 `approved`）。请求、批准和重复调用均幂等；
     Draft 修订后旧审批自动标记为非 current，不能为新 Draft 提供批准证明。
   - `ReportDraft confirmed`、`ReportApproval pending`、`ReportApproval approved` 是三个不同
     检查点。批准只覆盖报告内容，不修改 WorkItem 发布状态，不生成 Artifact，也不代表发布或交付。
   - 输出守卫要求“已提交审批/已批准”必须有本轮审批工具结果或当前 server-published 账本快照；
     stale、错版本和伪造 v999 继续 fail-closed，事件只记录 digest。
   - 补齐 Outline 确认后的确定性下一步 nudge，并约束 Draft 重放必须调用账本工具，不得从记忆复述正文。
   - schema 保持 rev8；Pack/Profile 升级为 `1.6.0` / `1.5.0`，`report-writing` 升级为 `1.2.0`。

   Mac mini 已在 `f58fa76` 完成审批合同、stale 失效、账本真实性及全量回归活体复核，
   验证文档为 `c793090`，M3-04A 正式关闭。

8. **M3-04B Artifact 渲染（已关闭）**：

   - 第一阶段只启用 DOCX；PDF 留到后续切片，避免把两套渲染器同时引入审批边界。
   - 只有当前 `ReportApproval approved + current=true`，且员工显式请求精确
     `生成 ReportDraft vN DOCX`，才能从绑定的 ReportDraft Markdown 渲染文件。
   - schema rev9 新增不可变 `enterprise_work_artifacts` 账本；Artifact 绑定 Draft digest、
     Approval digest、可信模板 digest 和 PackSnapshot，文件采用 sha256 内容寻址。
   - 数据库仅保存相对 storage key，不保存宿主机绝对路径；Artifact 元数据与
     `WorkItem.artifact_ids` 在同一事务登记，重复渲染同一输入保持幂等。
   - 渲染不等于发布或交付；状态固定为 `not_published` / `not_delivered`，M4 才引入
     delivery ledger、outbox 和员工文件投递。
   - Pack/Profile 升级为 `1.7.0` / `1.6.0`，`report-writing` 升级为 `1.3.0`；example
     和 cookiecutter 保持同步。

   Mac mini 已在 `85fb791` 完成 schema rev9、审批门、内容寻址 DOCX、幂等、stale、
   独立模板 smoke 和全量回归活体复核，验证文档为 `48e379f`。M3 从 Brief 到 Artifact
   的完整闭环正式关闭。

9. **M4-00 企微出站协议冻结（已关闭）**：

   - 当前部署固定为 AI Bot HTTP callback。官方 `response_url` 每个只可调用一次、有效期一小时，
     仅支持 `markdown` 与 `template_card`，不支持 `file`。
   - 官方 AI Bot 长连接支持临时素材上传、文件回复和主动推送，但与 callback 模式互斥；AgentSeek
     当前尚未实现该传输，不在 v0.1.0 临时切换。
   - `agentseek-wecom` 发布机器可读的两种传输能力矩阵，callback 下请求 `file` 必须 fail-closed；
     `response_url` 新增官方 `template_card` 发送形状。
   - 示例和模板提供 `probe_wecom_outbound.py`；production preflight 拒绝 callback + direct_file
     的无效组合，并只接受干净 HTTPS 基址的 `signed_link` 配置。
   - v0.1.0 决策：保留已验证 callback，M4 用模板卡片发送短期签名下载链接。消息投递 exactly-once
     与文件下载重试分开记账；直接附件留给后续长连接迁移。

   Mac mini 已在 `8b629ca` 完成能力矩阵、preflight、默认关闭的模板卡片活体探针及全量回归，
   验证文档为 `3ae928f`，M4-00 正式关闭。

10. **M4-01 发布合同（已实现并经 Mac mini 复核 PASS）**：

    - schema rev10 新增不可变 `enterprise_work_publications`，绑定 Artifact、Draft、Approval、
      模板摘要、政策、发布人和时间。
    - 只有当前 approved + current 内容、唯一当前 DOCX Artifact、物理文件哈希和企业身份全部复核通过，
      且员工精确输入 `发布 ReportArtifact vN`，才能发布。
    - 发布记录、WorkItem `published` 状态和 WorkEvent 在同一事务落账；精确重放不新增版本或事件。
    - 上游合同修订后旧发布保留历史但 `current=false`；伪造发布声明由独立守卫 fail-closed。
    - 发布不等于交付；本切片不生成卡片、文件、签名链接或下载端点，delivery 仍为关闭。
    - Pack/Profile/report-writing 升级为 `1.8.0` / `1.7.0` / `1.4.0`，example 与 cookiecutter 同步。

11. **M4-02 签名链接交付（Mac mini 活体 PASS，已关闭）**：建立 delivery ledger、一次性短期签名下载端点和
    template-card exactly-once 投递；审批、渲染、发布和交付继续保持分离。
12. **M4-03 浏览器侧下载 SSO（v0.1.x 后续安全加固，已延期）**：v0.1.0 保留已经活体验证的短时、
    recipient-bound、一次性 signed-link grant，不把尚未确定身份源的浏览器 SSO 作为 RC/GA 前置。
    后续实施前必须确定使用企业 OIDC/SAML 还是企微网页授权，以及 IdP 端点、client/agent 授权和
    HTTPS 回调域名；届时在 grant 之外校验浏览器身份并与 Delivery.recipient_key 绑定，不使用第二个
    bearer cookie 冒充身份认证。
13. **RC 可靠性收口（已完成）**：功能范围冻结后只处理阻断稳定性与账本幂等缺陷。Artifact 精确重渲染
    必须返回同一内容寻址记录，不新增 Artifact、事件或 WorkItem 版本；render/publish/deliver 的精确动作
    均由模型调用动作工具、服务端决定幂等，不由模型以只读查询替代动作。render/publish 的下一步命令由
    output guard 在模型漏转述时按本轮成功账本工具结果确定性补齐；RC 自动化与 Mac mini 活体验收矩阵见
    `V0.1.0_RC_IMPLEMENTATION.md`。

此前已完成原始本轮用户文本的显式 LangGraph state 传递，使裸“确认”的 fail-closed
backstop 在 live 路径生效。观测继续只保存 digest、长度、诊断信号和工具序列，不持久化
员工原文或守卫前报告正文。

正式研究必须建立在已确认的 ReportBrief 上。provisional ReportBrief 可以在
`draft/intake` 中保存和渐进补全，但不能触发知识检索或报告写作。

### 发布分段

```text
v0.1.0-alpha1  Profile、DigitalEmployeePack、WorkItem、WorkEvent、状态机和恢复
v0.1.0-alpha2  渐进式 ReportBrief、部门知识库、材料、证据和研究
v0.1.0-beta1   提纲、初稿、质量门和 Markdown
v0.1.0-rc1     短连接延迟回复、DOCX、批准、交付和故障恢复
v0.1.0-ga      一个限定行业报告主题端到端通过
```

不把 v0.1.0 GA 降级为只有任务账本的技术版本。

### 后续智能办公复用

`deepagents/enterprise-wecom` 保持通用 runtime 模板。行业报告能力以角色包存在，
不写死在 AgentSeek core、`agentseek-work` 或通用系统 prompt 中。

后续智能办公数字员工复用相同插件底座，并提供独立 smart-office 角色包：

```text
DigitalEmployeePack
  profile
  skills
  playbooks
  tool grants
  assets
  policies
  evals
```

Skills 负责方法和规范，Playbook 负责正式流程，Tools 执行动作，Policy 强制边界，
WorkItem 保存状态和证据。Skill 不能授予工具权限或替代审批、事务、egress 和审计。

v0.1.0 采用一个生成项目、一个企微 Bot、一个 Profile、一个启用 Playbook。
v0.1.1 保持一个独立企微 Bot 对应一名部门数字员工，在同一 Profile 下引入
Job Charter、服务目录和多个受治理 Playbook。运行时只向模型暴露当前选中
Playbook 的有效工具集，不把全部 Playbook 工具合并为一个无边界 Agent。

多数字员工共享单 gateway 的 profile-scoped agent registry 和跨岗位委派继续留作
后续架构，不作为 v0.1.1 前置。

### Sandbox 的新定位

sandbox 仍是 v0.1.0 的底层能力，但只为已授权的 WorkItem 服务。

沙箱不应简单设计成“每个员工一个常驻执行沙箱”。
更合适的模型是：

```text
员工级 workspace：按 tenant + employee 持久隔离文件和产物
任务级 sandbox：按 tenant + employee + session + task 临时创建执行环境
```

员工级 workspace 回答：

```text
这个文件属于哪个员工？
这个员工能不能访问这个文件？
这个生成物应该归属到哪里？
```

任务级 sandbox 回答：

```text
这次 PDF 解析、Excel 转换、报告生成、脚本执行在哪里安全运行？
本任务允许访问哪些输入？
执行超时多久？
输出哪些产物？
任务结束后如何清理？
```

推荐 scope：

```text
TaskSandbox(
  tenant_key=hmac-T,
  employee_key=hmac-E,
  session_key=hmac-S,
  task_id=task-001,
  inputs=[file_id_1, file_id_2],
  policy={
    timeout: 60s,
    network: false,
    max_memory: 512MB,
    max_output_bytes: 20MB
  }
)
```

首版可以实现：

```text
LocalTaskSandbox
```

它仍然使用宿主机本地目录，但必须满足：

- root 限定在 `runtime/sandboxes`；
- 每个任务一个临时 workspace；
- 只允许访问本任务显式传入的文件；
- 不暴露宿主机真实路径给模型；
- 禁止访问 `.env`、项目根目录和其他员工目录；
- 设置超时、最大输出大小和命令 allowlist；
- 任务结束后把产物登记回 `agentseek-files`；
- 成功或失败都写入脱敏观测事件。

后续可以替换为：

```text
DockerTaskSandbox
K8sJobSandbox
RemoteSandboxService
```

但上层接口保持：

```text
per task sandbox
employee scoped inputs
registered outputs
```

这样行业研究、报告写作和文件生成都站在同一个任务与隔离边界上。

## v0.1.1：部门数字员工与 Multi-Playbook Foundation

v0.1.1 把已经活体验证的“行业报告编写数字员工”演进为“一 Bot、一部门数字员工、
多 Skill、多受治理 Playbook”的扩展模型。完整方案见
`V0.1.1_DEPARTMENT_DIGITAL_EMPLOYEE_PLAN.md`。

首版范围：

- 建立 Digital Employee Job Charter，增加员工编号、展示名称、使命、服务目录和行为政策引用；
- 确定性回答“你是谁”“你能做什么”“怎么使用你”，并区分人类员工“我是谁”；
- 保留 `digital_employee_id=industry-report` 作为不可变技术身份，避免已有 WorkItem 失联；
- Profile v2 和 Pack 内 Playbook 权限声明向后兼容 v1 单 Playbook 配置；Pack schema 仍为 v1；
- 建立 Playbook Registry，移除运行时“Profile 必须恰好一个 Playbook”的限制；
- 建立 Profile 级统一能力池，Skills、文件、知识与 MCP 能力由普通对话和正式 Playbook 共享；
- Playbook 仅额外引入 WorkItem、合同、状态机、审批和审计，不建立第二套能力系统；
- 按“当前任务绑定、员工显式选择、确定性匹配、歧义澄清”在 Bot 内选择 Playbook；
- 使用测试型第二 Playbook 验证隔离，不在缺少业务合同的情况下上线第二个正式流程；
- 证券行业报告继续作为第一个生产 Playbook，v0.1.0 全部账本、状态机和交付协议保持不变。

当前状态：M0 合同冻结、M1 Job Charter、M2 Playbook Registry、M3 确定性内部路由、
M4-00 确定性服务响应、M4-01 统一 Capability Registry 和 M4-02 RC 收口均已通过
Mac mini 企微活体验收。RC 全生命周期与六项服务端可靠性加固已通过，版本已经合入
`production` 并发布 `enterprise-wecom-v0.1.1-ga`。
生产仍只启用证券报告 Playbook，第二 Playbook 继续只作为路由和隔离测试夹具。

v0.1.0 已完成的审批、发布和交付不在 v0.1.1 重做。尚未完成的多人评审、职责分离、
组织 RBAC、审批中心、SLA、任务运营和人工接管，放在 Multi-Playbook 基础稳定后的治理切片。

## v0.1.2：平台化加固与第二个部门数字员工

v0.1.2 不再重复建设已经存在的 research、content、DOCX、发布或交付链路。
本版本要回答两个问题：

1. 当前实现能否安全地复用于第二个部门业务服务；
2. 当前单实例样例能否演进为可由研发团队持续扩展和运维的平台。

### M0：当前状态与路线冻结

目标是让 Roadmap、GA 基线和研发文档描述同一套事实。

范围：

- 标记 v0.1.0 和 v0.1.1 已完成能力；
- 将早期 research/content 设想保留为历史，不再列为待实现主线；
- 公开当前权限、身份、运行时、下载和任务生命周期限制；
- 冻结 v0.1.2 的里程碑顺序与不做事项；
- 为第二个真实部门数字员工和 Playbook 选择业务负责人、输入、
  输出、审批人和验收样例。

验收：

- `ROADMAP.md`、`DEVELOPER_GUIDE.md` 和 GA 标签口径一致；
- 研发团队能够区分 Shared Capability、Skill 和 Playbook；
- 信息技术部数字员工与 `information-system-requirement-review@1`
  在编码前具备业务合同和验收 oracle。

M0 决策见 `V0.1.2_M0_PLATFORM_FREEZE.md`。首个交付物冻结为
《信息系统立项评估报告》；数字员工只提供立项建议，不代替正式立项审批。
本文档与配套研发指南完成后，由 Mac mini 执行文档一致性和模板回归复核；
复核通过即关闭 M0，再进入 M1，不在 M0 提前修改运行时代码或 schema。

### M0.1：企微协议基线

目标是在身份与组织授权平台化前，先固定企微会话边界和消息语义。

范围：

- 群聊按 `aibotid + chatid` 建立 session，单聊保持 `wecom:{userid}` 兼容；
- userid 解密只更新成员身份，不覆盖群聊会话边界；
- 将引用消息的安全语义传入 Agent，不暴露 `response_url` 或签名媒体 URL；
- 厘清 callback、long connection 和自建应用的边界，修正现有
  AI Bot 能力声明与协议测试。

验收：

- 同一成员的不同群聊不共享 session，不同机器人也不共享群 session；
- 引用文本对 Agent 可见，敏感回调能力不进入 prompt 上下文；
- 企微组件和企微数字员工样例回归通过。

详细决策、官方协议基线和非目标见 `V0.1.2_M0_1_WECOM_PROTOCOL_BASELINE.md`。
M0.1 是 Transport Kernel 的前置安全切片，不改变 M1–M5 的业务范围。

### M0.2：企微 Transport Kernel

目标是在增加长连接和自建应用前，将现有 Callback 接入从单体
`WeComChannel` 中拆出，并建立三种渠道共用的会话地址合同。

范围：

- 定义 `WeComTransport` 生命周期、入站绑定和地址解析接口；
- 定义 `ConversationAddress`，统一 tenant、Bot/Agent、transport、会话、
  发送人、明文成员、最近交互时间和回复期限；
- 实现 `AiBotCallbackTransport`，接管 HTTP 路由、消息加解密和 Uvicorn
  生命周期；
- `WeComChannel` 保留消息语义、会话队列和 Agent 编排，通过 Transport
  接收入站消息；
- 保持 Callback 路径、健康响应、单聊 session、群聊隔离、流式回复和
  `response_url` 行为兼容。

非目标：

- 不实现 AI Bot 长连接或自建应用；
- 不增加数据库 schema，不持久化 inbox、outbox 或 msgid 去重；
- 不改变现网 `.env`、Bot 配置或部署端口；
- 不改变文件队列和卡片交互行为，这些进入后续渠道切片。

验收：

- Callback HTTP 和加解密实现不再位于 `WeComChannel`；
- 单聊身份解密前后保持既有 session 兼容；
- 群聊地址不因成员身份解密而改变；
- 地址上下文不得包含 `response_url`、签名媒体 URL 或其他回复能力；
- 企微组件、企微数字员工样例、Ruff 和 ty 全部通过；
- Linux 使用现有 Callback 配置完成单聊、群聊、引用和脱敏活体回归。

设计说明见 `V0.1.2_M0_2_WECOM_TRANSPORT_KERNEL.md`。M0.2 通过后再进入
持久化消息基座；长连接和自建应用继续保持关闭，直到各自切片通过验收。

### M0.3：企微持久化消息基座

目标是在不迁移共享数据库的前提下，为所有企微 Transport 固定 inbox、
`msgid` 去重、outbox、回复期限和处理 lease 合同。

范围：

- 定义 `DurableMessageStore`，先提供单机加密 SQLite 适配器；
- 入站在执行 Agent 前持久化，并按 tenant、Bot/Agent、transport 和 `msgid` 去重；
- `response_url` 终态回复在网络调用前进入 outbox；
- 启动时先恢复 outbox，再恢复仍有回复能力的 inbox；
- SIGTERM 由 Bub manager 统一编排，优雅停止排空终态提交并释放 owner lease；
- 异常终止依靠 lease 到期，并由周期恢复任务自动接管；
- 默认保持 memory 模式，Linux 活体复验显式启用 sqlite。

非目标：

- 不迁移或修改共享 PostgreSQL、Work、Memory、ContextSeek schema；
- 不在本切片实现多主机共享存储；
- 不自动恢复包含进程内业务回调的模板卡片；
- 不启用长连接或自建应用。

验收：

- 重启后相同 `msgid` 不再次执行 Agent；
- 仍在期限内的 Markdown outbox 和 inbox 可恢复；
- 消息正文、回复能力和原始 `msgid` 不以明文落盘；
- SQLite 文件为 `600`，错误密钥、符号链接和 schema 漂移 fail closed；
- 默认 memory 模式和 M0.2 单聊、群聊、引用、流式行为零回归。

设计与复验分别见 `V0.1.2_M0_3_WECOM_DURABLE_MESSAGING.md` 和
`V0.1.2_M0_3_WECOM_DURABLE_VERIFICATION.md`。

### M0.4：Callback 硬化与卡片交互

目标是收口 Callback 剩余的耗时 I/O 和可恢复卡片状态机。

当前状态：Linux Callback 活体复验 PASS，已合入 `production`。

范围：

- 文件解析完成后统一回到 session 队列，不绕过串行执行边界；
- 首包前不执行媒体下载、身份网络解析或其他不可控耗时 I/O；
- 接入模板卡片按钮、投票和多选事件；
- 将卡片投递成功后的业务提交改为可持久化幂等动作，不依赖进程内闭包；
- 完成模板卡片 outbox 的 `sending`、`sent`、`delivered` 重启恢复和
  `blocked` 人工对账状态。

M0.4 不改变 Transport 类型，也不提前实现 WebSocket 或应用主动推送。
设计与复验分别见 `V0.1.2_M0_4_WECOM_CALLBACK_HARDENING.md` 和
`V0.1.2_M0_4_WECOM_CALLBACK_VERIFICATION.md`。

### M0.5：AI Bot 长连接 Transport

目标是实现 `AiBotLongConnectionTransport`，并让长任务、定时提醒和
已交互会话主动状态通知使用长连接主通道。

当前状态：代码与确定性门禁已完成，等待使用新长连接机器人和隔离 Linux
canary 的并行活体复验；现有 Callback 机器人不切换模式。

范围：

- WebSocket 建连、鉴权、心跳、断线重连和单实例所有权；
- 24 小时会话回复期限和已交互会话主动推送资格；
- `aibot_send_msg` Markdown/Card 主动发送；
- 复用 `ConversationAddress`、持久化 inbox/outbox 和群/单聊隔离合同；
- 与 Callback 配置互斥，并保留 Callback 作为回退通道。

Linux 活体验收通过前，不合入 `production`，也不建立发布 tag。
设计与切换复验分别见 `V0.1.2_M0_5_WECOM_LONG_CONNECTION.md` 和
`V0.1.2_M0_5_WECOM_LONG_CONNECTION_VERIFICATION.md`。

渠道身份和业务身份保持分离：BotID/AgentID 表示企微入口，
`digital_employee_id` 表示员工正在对话的数字员工。当前
`wecom:<userid>` 适用于“一实例一数字员工”，可以隔离同一数字员工服务的
不同员工。未来多个数字员工共享 gateway 或短期记忆数据库时，允许共用
物理数据库，但逻辑键必须包含 `tenant_id + digital_employee_id +
conversation_id`。不能用 BotID 代替 `digital_employee_id`，否则同一数字员工
更换机器人或迁移 Transport 会失去业务会话连续性。

### M0.6：企微自建应用 Transport

目标是实现 `WeComAppTransport`，承担真正的指定成员、部门和标签主动通知。

范围：

- 独立的应用消息与事件回调；
- access token 缓存、刷新、错误码处理和应用可见范围检查；
- 指定成员、部门、标签的文本、图片、视频、文件、图文和卡片推送；
- 应用 Agent ID 纳入统一会话地址和审计；
- 复用持久化 outbox，并对主动推送建立幂等和重试边界。

自建应用不提供 AI Bot 流式回复。它是长连接的企业主动通知补充通道，
不是 Callback 的透明替代。

目标部署允许一个部门配置一名或多名数字员工。每名数字员工独立绑定一个
Profile 和一个 AI Bot；每个 Bot 在 Callback 与长连接中二选一。一个企业级
自建应用可以作为这些数字员工共享的主动通知出口，但每条 outbox 必须携带
来源 `digital_employee_id`，并校验应用可见范围、目标成员/部门/标签和幂等键。
当前 `CORP_ID/APP_SECRET` 只服务 userid 转换等辅助 API，不代表该公共应用
Transport 已实现。

### M1：组织授权与身份 Provider 抽象

目标是先解决跨部门复用时最重要的授权边界。

范围：

- 用稳定组织 ID、组织路径 ID 和成员关系替代部门名称子串判断；
- 建立类型化 requester scope evaluator，不再硬编码单一
  `strategic-development-employee` 分支；
- 保持 `Profile ∩ 部署能力 ∩ 员工授权 ∩ Policy` 的权限上限；
- 建立 Identity Provider Registry，保留 DM Provider，并允许接入 OIDC、LDAP
  或企业 HTTP 身份服务；
- 增加跨部门、相似部门名称、缺失身份和 Provider 故障的 fail-closed 测试；
- 保持现有 `digital_employee_id`、WorkItem 和历史合同可读，不做破坏式迁移。

验收：

- 不再以组织展示名称的子串作为授权依据；
- 未授权员工不能发现、创建、读取或推进其他部门 WorkItem；
- Provider 不可用时不降级为匿名授权；
- v0.1.1 证券报告全生命周期零回归。

### M2：Playbook 与 Capability 扩展 SDK

目标是让研发团队增加能力和正式服务时遵循稳定接口，而不是复制报告代码。

范围：

- 定义类型化 `CapabilityDescriptor`、业务工具适配器和 Policy 元数据；
- MCP server/tool 的固定映射由注册描述生成，仍不向模型暴露通用
  `call_mcp_tool`；
- 将 Playbook 的 intent、依赖、Work composition、合同、Guard 和状态摘要
  收敛为明确扩展接口；
- 按合同类型拆分过大的 Composition 和 Output Guard 策略；
- 为 cookiecutter 模板增加 Profile、Capability、Playbook、权限和路由测试骨架；
- 提供一个只用于开发验证的最小第二 Playbook fixture。

验收：

- 新增一个共享 MCP 能力主要通过描述和业务适配器完成；
- 新 Playbook 不修改证券报告私有模块即可完成注册和隔离测试；
- Playbook 不能扩大 Profile 权限，也不能绕过确认、审批和审计；
- 模板生成项目自带可运行的扩展回归测试。

### M3：信息技术部数字员工与需求评审 Playbook

目标是用第二个真实部门部署证明模板、身份、授权、Capability 和
Playbook 治理可跨部门复用，而不是复制证券报告。

服务已冻结为 `information-system-requirement-review@1`，交付
《信息系统立项评估报告》。它应复用文件分析、信息技术部知识和经员工
明确授权的外部公开信息，但使用独立的：

- 服务目录和确定性路由词；
- WorkItem playbook ID；
- 输入合同、输出合同和人工检查点；
- Policy、审批角色和质量门；
- 状态摘要、Guard 和验收数据。

验收：

- 战略发展部与信息技术部数字员工必须使用独立 Bot、Profile、Pack 和稳定技术身份；
- 两个部署的合同、工具、数据和 Artifact 互不串扰；
- Shared Capability 由同一 SDK 和注册接口构建，不复制证券报告私有模块；
- 同 Bot Multi-Playbook 仍由 M2 fixture 验证当前任务优先、确定性选择和歧义澄清；
- Mac mini 完成跨部门、跨员工和重启后的活体复验。

### M4：生产运行时加固

目标是从 Mac mini 单实例稳定运行演进到可恢复、可监控的生产部署。

范围：

- 将 session queue、stream ownership、msgid dedup 和 in-flight turn lease
  持久化到 Redis 或 PostgreSQL；
- 支持进程重启后的在途任务恢复、超时终止和重复回调幂等；
- 定义多实例同会话单飞和实例失效接管协议；
- 增加 `/health/live`、`/health/ready` 和关键依赖 readiness；
- 增加队列深度、拒绝、超时、模型调用、MCP 和交付指标；
- 提供容器或标准服务管理部署样例，保留现有 Mac mini 运行方式。

验收：

- gateway 重启不丢失已接收任务，也不重复执行正式动作；
- 多实例下同一会话仍严格串行，不同会话可以并行；
- PostgreSQL、身份 Provider、模型或关键 MCP 不可用时 readiness 准确失败；
- Langfuse 不可用继续保持业务 fail-open、观测有界降级。

### M5：安全、质量与任务生命周期

目标是在扩大使用范围前补齐高价值治理能力。

范围：

- 对机密 Artifact 下载接入 OIDC、SAML 或企微网页认证，并将浏览器身份绑定
  到 delivery recipient；在企业 IdP 未确定前保留当前一次性短时 Token；
- 建立 Claim reviewer、事实核验状态、来源冲突和未解决问题处理流程；
- 区分“内容已批准”和“事实已核验”，不以 ReportApproval 代替事实验证；
- 设计 `archived` 或等价的正式结束动作，区分“修订当前任务”和“新建独立任务”；
- 增加任务查询、人工接管、审计检索和过期数据治理入口。

验收：

- 未通过浏览器身份校验的接收人不能下载受保护 Artifact；
- 报告能够展示 Claim 的核验状态和未解决风险；
- 员工可以显式归档已交付任务，再创建同 Playbook 的新任务；
- 历史 WorkItem、合同、Artifact、Publication 和 Delivery 保持不可变可审计。

### v0.1.2 不做事项

- 不使用 LLM-first 路由静默选择正式 Playbook；
- 不恢复允许模型拼接 MCP server/tool 名的通用工具；
- 不在一个 Bot 内隐藏多名数字员工；
- 不让 Skill 或 MCP 调用替代 WorkItem、合同、确认、审批和审计；
- 不为了支持第二 Playbook 复制整套证券报告 Composition；
- 不在缺少企业 IdP 和 HTTPS 回调域名时伪造浏览器 SSO；
- 不把 PDF、XLSX、PPTX 等格式扩展置于权限和第二 Playbook 验证之前。

## 后续候选能力

以下能力保留在 backlog，按真实部门需求进入后续版本：

- PDF、XLSX 和 PPTX 产物，以及公司品牌模板；
- 图表、流程图和组织架构图的结构化理解；
- 文件长期化、团队知识治理和文档系统归档；
- 多人评审、职责分离、统一审批中心和 SLA；
- 多数字员工服务台、跨岗位委派和跨 Bot 能力发现；
- 任务、记忆、文件、MCP 和审计管理后台。

## 总体路线

```text
v0.0.8  企业身份、记忆、MCP、审计和观测 runtime
v0.0.9  文件输入、OCR、Office/PDF 理解和大文件分析
v0.1.0  证券行业报告 WorkItem 全生命周期与 signed-link 交付
v0.1.1  部门数字员工、Job Charter、统一能力池和 Multi-Playbook Foundation
v0.1.2  组织授权、扩展 SDK、第二个部门数字员工和生产运行时加固
后续     多人治理、多数字员工协作、多格式产物和管理后台
```

最终目标不是继续增加互不关联的工具，而是让研发团队能够在统一的身份、权限、
能力、任务和审计边界内，持续交付新的部门数字员工服务。
