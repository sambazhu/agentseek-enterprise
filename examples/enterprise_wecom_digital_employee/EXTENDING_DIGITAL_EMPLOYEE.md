---
title: 扩展数字员工的 Skill、MCP 和 Playbook
type: how-to
audience: [A2, A3]
runs: yes
verified_on: 2026-08-03
sources:
  - V0.1.2_M0_PLATFORM_FREEZE.md
  - src/enterprise_wecom_digital_employee/capability_catalog.py
  - src/enterprise_wecom_digital_employee/capability_registry.py
  - src/enterprise_wecom_digital_employee/playbook_registry.py
  - src/enterprise_wecom_digital_employee/pack_loader.py
  - src/enterprise_wecom_digital_employee/reports/playbook.py
  - digital_employees/industry-report/profile.yaml
  - digital_employees/industry-report/pack.yaml
  - tests/test_capability_registry.py
  - tests/test_playbook_registry.py
  - tests/test_pack_loader.py
---

# 扩展数字员工的 Skill、MCP 和 Playbook

本指南说明如何在不破坏权限、账本和模板一致性的前提下，为一名部门数字员工
增加通用能力或正式服务。

## 先选择正确扩展点

在写代码前回答以下问题：

1. 只是增加方法、规范、术语或操作步骤吗？增加 Skill。
2. 需要读取知识库、数据平台或调用业务系统吗？增加 MCP 业务能力。
3. 需要长期任务、版本、确认、审批、发布或交付吗？增加 Playbook。
4. 需要在多个模板中复用新的 Channel、Hook 或存储后端吗？增加 contrib Plugin。

Skill 和 MCP 属于同一个统一能力池，普通协助与正式 Playbook 都可以使用。Playbook
的价值是治理，而不是复制一套专属 Skill/MCP。

## 修改 Job Charter

当能力改变数字员工对外身份或职责时，先修改：

```text
digital_employees/industry-report/profile.yaml
```

常见字段：

```yaml
mission: 基于授权知识和可审计证据完成部门工作
responsibilities:
  - 行业资料收集和证据整理
service_catalog:
  - service_id: securities-report
    title: 证券行业正式报告
    playbook_ref: securities-industry-report@1
behavior_principles:
  - 重要事实和结论会保留来源
```

面向员工的文案使用业务语言，不写内部工具名、MCP server、合同字段或模型实现。
Profile 改变后升级 `profile_version`；如果它属于 Pack 内容，也必须升级
`pack_version`。

## 增加一个 Skill

### 1. 创建 Skill

正式 Pack 使用的 Skill 放在数字员工 Pack 内：

```text
digital_employees/industry-report/skills/<skill-id>/
  SKILL.md
  references/
```

`SKILL.md` 应写清适用场景、输入、步骤、边界和输出。复杂模板或参考资料放在
`references/`，由 Skill 相对引用。

### 2. 计算内容摘要

在 macOS 上计算主文件 SHA256：

```bash title="not executed in this run"
shasum -a 256 \
  digital_employees/industry-report/skills/<skill-id>/SKILL.md
```

该命令含项目自定义占位符，本次未执行。`<skill-id>` 是示意占位符，不要原样提交。

### 3. 登记到 Pack

```yaml
skills:
  - id: department-analysis
    version: 1.0.0
    path: skills/department-analysis/SKILL.md
    sha256: <64-character-sha256>
```

在 Profile 的 `skill_refs` 中声明它。需要该 Skill 的 Playbook 也在自己的
`skill_refs` 中声明，且必须是 Profile 集合的子集。

### 4. 升级版本

- Skill 内容改变：升级 Skill 版本并更新 SHA。
- Profile 引用改变：升级 `profile_version`。
- Pack 任意内容改变：升级 `pack_version`。
- 更新所有 `skill://<id>@<version>/...` 引用。

Restricted Pack Loader 会验证文件存在、UTF-8、SHA、相对路径和权限子集。不要
通过软链接或仓库外绝对路径绕过 Pack。

### 5. 测试

至少覆盖：

- Pack 可加载；
- SHA 或引用错误时 fail-closed；
- Skill 引用不超出 Profile；
- DirectTurn 和需要它的 Playbook 都能看到对应业务能力；
- 未声明的 Playbook 不能使用它。

## 增加一个 MCP 业务能力

### 1. 配置 MCP server

本机配置写入 `.agents/mcp.local.json`。以下仅为无凭据结构示例：

```json
{
  "mcpServers": {
    "department-metrics": {
      "command": "uv",
      "args": ["run", "python", "-m", "company_metrics.mcp_server"]
    }
  }
}
```

生产凭据通过环境变量或密钥管理注入。不要把 token 写在 JSON 后提交。

### 2. 定义稳定业务工具

在 `capability_registry.py` 中增加员工可理解的工具，例如：

```python
@tool("search_department_metrics")
async def search_department_metrics(query: str, runtime: ToolRuntime) -> str:
    return await invoke_mcp(
        "department-metrics",
        "search_metrics",
        {"query": query},
        False,
    )
```

要求：

- server 和 remote tool 使用服务端固定映射；
- 不向模型暴露通用 server/tool 拼接参数；
- 工具名表达业务含义，不泄漏内部平台名时优先使用能力名称；
- 对外部数据或写操作执行员工授权和策略检查；
- 在正式 Playbook 中需要登记来源时，拒绝普通直连并引导使用 Playbook 工具。

### 3. 声明运行时可用条件

在 `capability_catalog.py` 中把能力定义为以下交集：

```text
Profile tool_grant
  ∩ Profile data_scope
  ∩ MCP server 已配置
```

Job Charter 只介绍当前真实可用的能力。没有配置 server 时，不应仍向员工宣称
“可以使用”。

### 4. 更新 Profile

按需要增加：

```yaml
tool_grants:
  - department-metrics-read
data_scopes:
  - department-approved-metrics
```

知识库还应增加 `knowledge_refs`，明确 owning org、collection、检索模式和
remote tools。Playbook 如需使用，相同 grant/scope 也要声明在 Playbook spec，
且不能超出 Profile。

### 5. 配置企业 MCP 策略

对照 `agentseek-enterprise` 的 MCP README 配置：

- allowlist / denylist；
- risky/write/confirm tools；
- 是否需要本轮明确确认；
- JSONL 审计路径；
- 参数和结果脱敏。

工具可见不等于已获授权。服务端调用时仍要执行策略。

### 6. 测试

至少覆盖：

- server 未配置时能力不出现；
- grant 或 scope 缺失时能力不出现；
- DirectTurn 可以调用；
- Playbook 可以通过同一 registry 调用；
- 正式工作中普通直连 fail-closed；
- 外部能力无明确员工请求时零 MCP 调用；
- 固定 server/tool 名与审计记录一致；
- 模型工具列表中没有 `call_mcp_tool` 和 `list_mcp_tools`。

## 增加一个正式 Playbook

### 1. 定义服务

在 Profile 的 `service_catalog` 中增加真实业务服务：

```yaml
service_catalog:
  - service_id: information-system-requirement-review
    title: 信息系统需求评审与立项评估
    summary: 评估信息系统需求并形成可审批、可交付的立项建议
    playbook_ref: information-system-requirement-review@1
    workflow_steps:
      - 需求确认
      - 材料与证据核验
      - 多维评估与立项建议
      - 审批与文件交付
    example_requests:
      - 请启动信息系统需求评审与立项评估
```

同时把 `information-system-requirement-review@1` 加入信息技术部 Profile 的
`supported_playbooks`。这项服务属于独立的信息技术部数字员工，不要把它加入
战略发展部 Profile。冻结的输入合同、审批边界和验收样例见
[`V0.1.2_M0_PLATFORM_FREEZE.md`](V0.1.2_M0_PLATFORM_FREEZE.md)。

不要为了证明多 Playbook 能运行而发布一个假的生产服务。测试夹具应只放在测试中。

### 2. 实现 Binding

新增业务包，例如：

```text
src/enterprise_wecom_digital_employee/it_requirements/
  playbook.py
  composition.py
  tools.py
  output_guard.py
```

Playbook factory 必须返回符合 `PlaybookBinding` Protocol 的对象，核心接口包括：

```python
class PlaybookBinding(Protocol):
    spec: PlaybookSpec
    playbook_ref: str
    pack_snapshot_id: str

    def authorize_state(self, state): ...
    def enrich_state(self, message, session_id, state): ...
    def tools(self): ...
    def guard_output(self, result, output): ...
    def current_work(self, state, runtime_context=None): ...
    def introduction(self): ...
    def instructions(self): ...
    def direct_response(self, message, state, runtime_context=None, callbacks=()): ...
```

Entrypoint 只能位于允许的项目 Python package 内，外部任意模块会被 loader 拒绝。

### 3. 登记 Pack

```yaml
playbooks:
  - id: information-system-requirement-review
    version: "1"
    entrypoint: enterprise_wecom_digital_employee.it_requirements.playbook:build_playbook
    skill_refs:
      - information-system-requirement-review@1.0.0
    policy_refs:
      - information-system-requirement-review-v1
    tool_grants:
      - analyze_file
      - department-knowledge-read
    data_scopes:
      - requester-authorized-files
      - department-approved-knowledge
    routing:
      explicit_aliases:
        - 信息系统需求评审与立项评估
        - IT 需求评审
      intent_terms:
        - 系统需求评审
        - 立项评估
      owned_command_terms:
        - systemrequirementbrief
        - initiationrecommendation
        - 需求评审任务状态
      priority: 90
```

上述 `skill_refs`、grant 和 scope 是最小示例。实际 Pack 只能引用信息技术部
Profile 已声明且部署中真实可用的能力；不得复制证券报告的专属研究模板或
默认引入专业证券数据源。

每个 `skill_refs`、`policy_refs`、`tool_grants` 和 `data_scopes` 都必须是 Profile
声明的子集。Registry 要求 Profile 声明和实际 Binding 一一对应。

### 4. 设计路由

路由词分三类：

- `owned_command_terms`：该 Playbook 独占的精确合同或动作词；
- `explicit_aliases`：员工明确选择服务时使用的服务名；
- `intent_terms`：确定性、唯一的业务意图词。

避免与已有 Playbook 产生宽泛重叠。两个服务同时匹配时应返回澄清，不要用 LLM
暗自选择。为 active Work 的自然续接和肯定应答增加测试。

### 5. 设计账本和状态机

只在确实需要持久治理时新建合同或 schema：

- 输入合同：员工确认了什么；
- 过程记录：使用了哪些来源和授权；
- 输出合同：生成了哪个不可变版本；
- 审批合同：谁在何时批准；
- Artifact、Publication、Delivery：文件、发布和交付彼此分离。

所有写操作必须幂等，并对 tenant、requester、work 和 expected version 做校验。
数据库变化使用正式 migration；不得让部署人员手工更新业务状态。

### 6. 增加确定性服务端兜底

以下行为不要完全依赖模型：

- 精确确认、批准、渲染、发布和交付命令；
- 状态查询；
- 多步但固定的初稿生成准备；
- 关键下一步提示；
- marker、token 和内部指令过滤。

模型适合生成内容和解释，不适合作为状态机或账本真相源。

### 7. 编写测试

Playbook 最少测试：

- Profile/Pack/Binding 加载；
- 路由 exact/explicit/match/ambiguous/no-match；
- 请求人不在范围时 fail-closed；
- 权限提升被拒；
- 仅选中的 Binding 被调用；
- 普通对话不意外创建 WorkItem；
- 合同 create/confirm/revise/幂等/并发/stale；
- 输出守卫正负向；
- 跨重启读取；
- 与现有部门部署隔离；同 Bot Multi-Playbook 另用测试 fixture 验证；
- 企微 burst、队列和 exactly-once 不回归。

## 何时增加 contrib Plugin

满足以下条件才考虑 `contrib/agentseek-<name>`：

- 能被多个模板或项目复用；
- 需要 Bub plugin entrypoint 或 Hook；
- 有独立依赖、配置模型和运行时生命周期；
- 不包含某部门专属工作流。

新 Plugin 应自带：

- `pyproject.toml` 和 `[project.entry-points.bub]`；
- README：用途、安装、配置、运行、验证、限制；
- 类型检查和测试；
- `contrib/README.md` 索引项。

## 同步 enterprise-wecom Template

如果改动应出现在以后创建的所有数字员工项目中，必须同步：

```text
templates/deepagents/enterprise-wecom/{{cookiecutter.project_slug}}/
```

示例和模板中的文件通常需要保持功能等价。同步后运行：

```bash
PYTHONPATH=. uv run pytest \
  tests/cli_commands/test_templates_render.py \
  -k "enterprise and wecom" -q
```

再在临时目录真实创建一次：

```bash
agentseek create ./templates/deepagents/enterprise-wecom --no-input
```

检查生成目录中没有残留 `{{ ... }}`，并运行 `agentseek info`、`agentseek doctor`。

## 版本升级速查

| 改动 | 必须处理 |
| --- | --- |
| Skill 内容 | Skill version、SHA、引用、Pack version |
| Profile 身份/权限/服务 | Profile version、Pack version |
| Pack 中任一文件 | Pack version，保留旧 PackSnapshot |
| Playbook 不兼容合同语义 | Playbook version、路由和兼容策略 |
| 数据库结构 | schema revision、migration、旧数据兼容测试 |
| 新模板默认能力 | 示例与 Cookiecutter 同步、render test |
| 只改文档 | 通常不升运行时版本，但运行 docs-test |

完成扩展后，按[研发分支、验证与合并流程](DEVELOPMENT_WORKFLOW.md)提交。
