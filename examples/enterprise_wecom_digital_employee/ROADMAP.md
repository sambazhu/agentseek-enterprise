---
title: Enterprise WeCom Evolution Roadmap
type: explanation
audience: [A2, A3, A4]
runs: no
verified_on: 2026-07-12
sources:
  - examples/enterprise_wecom_digital_employee/README.md
  - examples/enterprise_wecom_digital_employee/PRODUCTION_FREEZE.md
  - examples/enterprise_wecom_digital_employee/V0.1.0_INDUSTRY_REPORT_DIGITAL_EMPLOYEE_PLAN.md
  - examples/enterprise_wecom_digital_employee/V0.1.0_M0_FREEZE.md
  - docs/concepts/enterprise-wecom-template.zh.md
  - templates/deepagents/enterprise-wecom/README.md
---

# Enterprise WeCom 演进路线

本文记录 `enterprise-wecom` 在 v0.0.8 GA 之后的定位澄清和路线规划。

## 当前定位

`enterprise-wecom` 当前已经是企业数字员工的 runtime harness 和项目脚手架。

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

所以 v0.0.8 之后的 `enterprise-wecom`，已经不只是一个聊天示例。
它是可部署、可观测、可审计的企业数字员工运行底座。

当前最强的路径是：

```text
员工发消息
-> 识别员工身份
-> 注入组织、岗位、记忆上下文
-> 调用企业 MCP 工具
-> 执行 policy/audit
-> 观测和脱敏
-> 回复员工
```

这说明它已经具备企业 MCP 调用能力和 runtime 治理能力。

目前较弱的能力是：

- 文件输入和文件输出；
- 多步 research；
- 内容生产；
- 报告、PPT、Word、Excel 等交付物生成；
- 更面向团队的业务工作流。

后续路线应从“企业工具调用型数字员工”升级为“企业任务完成型数字员工”。

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

### 发布分段

```text
v0.1.0-alpha1  Profile、DigitalEmployeePack、WorkItem、WorkEvent、状态机和恢复
v0.1.0-alpha2  ReportBrief、材料、证据和研究
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

v0.1.0 采用一个生成项目、一个企微 Bot、一个 Profile、一个启用角色包。
未来多数字员工单 gateway 需要 profile-scoped agent registry 和 SkillResolver，
不能把所有岗位 Skills 同时暴露给一个 agent。

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

## v0.1.1：企业授权、审批与运营

v0.1.1 建议增强组织级治理：

- 在 v0.1.0 简化角色映射基础上支持可选的多人评审和职责分离；
- 组织 RBAC 和数据范围；
- 职责分离；
- 审批中心；
- SLA、超时和升级；
- 任务管理和人工接管；
- 质量、成本和风险指标。

## v0.1.2：受治理的企业 research workflow

企业 research 不作为脱离 WorkItem 的自由 Agent。
它在 `enterprise-wecom` 的报告 Playbook 内执行。

推荐结构：

```text
research/
  deep_research.py
  prompts.py
  schemas.py
  report.py
```

首版工具：

```text
run_enterprise_research(objective, file_ids=None, output_format="summary")
```

能力范围：

- 读取当前附件摘要；
- 使用 Tavily / gildata / 企业 MCP；
- 形成结构化 notes；
- 输出 Markdown 研究报告草稿；
- Langfuse 记录 research step metadata；
- 不直接生成 docx/pdf。

目标是让数字员工能执行：

```text
plan -> search/query -> read -> synthesize -> answer
```

## v0.1.3：企业 content 与报告产物

v0.1.3 可以吸收 `content-builder` 的思想，实现企业内容生产工作流。

推荐结构：

```text
content/
  planner.py
  writer.py
  reviewer.py
  exporter.py
  schemas.py
```

首版工具：

```text
create_enterprise_content(
  objective,
  audience=None,
  format="wechat_reply|markdown|docx|ppt_outline",
  source_file_ids=None,
  use_research=True
)
```

支持场景：

- 写汇报材料；
- 写制度解读；
- 写培训稿；
- 写会议纪要；
- 写 PPT 大纲；
- 按受众调整语气；
- 做合规审查。

输出链路可以设计为：

```text
content result
-> file generator
-> agentseek-files outbound
-> agentseek-wecom send file
```

## v0.1.4 及以后

后续可以继续扩展：

### 更多文件输出格式

- 在 v0.1.0 DOCX 报告交付基础上扩展 PDF、XLSX 和 PPTX；
- 支持公司级模板、样式和品牌规范；
- 支持文档系统归档和跨渠道交付。

### 高级图片和图表理解

- 在 v0.0.9 OCR 基础上增加非文字图片描述；
- 理解流程图、组织架构图和复杂图表；
- 对图形化数值生成可验证的结构化数据；
- 保留图片区域、OCR 结果和报告引用之间的对应关系。

### 文件长期化

- 员工明确要求“长期记住这份材料”；
- 文件摘要进入显式长期记忆；
- 文件 chunks 进入 pgvector；
- 按员工和团队隔离。

### 多团队接入

- team profile；
- team MCP 配置；
- team prompt；
- team memory namespace；
- team policy；
- team observability 标签。

### 管理后台和运维工具

- 查询员工记忆；
- 删除错误记忆；
- 查询文件记录；
- 清理过期文件；
- 查看 pgvector 命中；
- Langfuse 指标看板。

## 总体路线

v0.0.8 完成企业数字员工 runtime 底座，v0.0.9 完成文件输入与分析。
v0.1.0 开始进入岗位和任务完成层：

```text
v0.0.8  可观测、可部署、可审计的企业 runtime
v0.0.9  文件输入、OCR、Office/PDF 理解和大文件分析
v0.1.0  行业报告数字员工、WorkItem、Playbook、证据、评审和报告交付
v0.1.1  企业授权、正式审批、任务运营和人工接管
v0.1.2  受 WorkItem 治理的 enterprise research
v0.1.3  企业 content、模板化报告和多格式产物
v0.1.4  团队任务队列、岗位协作和跨数字员工移交
```

最终目标是把 `enterprise-wecom` 从：

```text
企业工具调用型数字员工
```

升级为：

```text
企业任务完成型数字员工
```

它不仅能调用 MCP 查数据、办流程，还能接收材料、研究问题、生成内容、
输出文件，并且全程带身份、权限、记忆、审计和观测。
