---
title: 执行隔离与沙箱
type: explanation
audience: [A2, A3, A4]
runs: no
verified_on: 2026-09-06
sources:
  - docs/concepts/enterprise-wecom-architecture.zh.md
  - examples/enterprise_wecom_digital_employee/ROADMAP.md
  - examples/enterprise_wecom_digital_employee/V0.1.3_EXECUTION_ISOLATION_PLAN.md
---

# 执行隔离与沙箱

执行隔离限制一次 Skill、Tool 或 Playbook 动作可以访问的文件、网络和资源，
并把执行进程的故障与 Gateway 分开。v0.1.3 当前方案选择 CubeSandbox 一个后端，
仍处于架构评审阶段，尚未实现或部署。

## 部署、数据与执行边界

一个逻辑部署单元代表一名数字员工，可服务多名员工并拥有多个 Playbook。
执行隔离在这一模型下增加按任务租用的微虚机：

```text
Gateway / 企业主 Agent
  -> Execution Broker（授权、租约、导入导出、对账）
       -> CubeSandbox：每个 execution attempt 独占一张 VM
       -> Execution Ledger + Content Store
```

主 Agent 保留身份、记忆、WorkItem 和消息交付。专用执行 Tool 或子 Agent
获得沙箱接口；主 Agent 不默认获得通用 Shell。DeepAgents Backend 提供文件/命令
接口，企业授权和生命周期由 Broker 控制。

Cube 服务可以部署在独立内网 KVM 节点，数字员工的 Gateway 可继续部署在普通 VM。
共享 Cube 集群时仍需验证各服务身份的资源授权，不能把物理共用理解成数据共用。

## VM 粒度与工作进度

一个 execution attempt 可执行多个连续命令，例如写脚本、运行、修正和导出。
审批等待和阶段完成后释放 VM；下一阶段使用新 VM，加载已提交文件版本。
普通 Turn 以 task_id 关联文件工作，不要求创建正式 WorkItem。

任务数据比 VM 活得更久：

| 数据 | 隔离粒度 | 保存方式 |
| --- | --- | --- |
| Skill/模板 | 数字员工 + 发布版本 | 不可变资产，只读 |
| 背景资料/附件 | 授权文件清单 + 内容版本 | /context、/inputs，只读导入 |
| 工作副本 | task/work + revision + attempt | /workspace，attempt 单写 |
| 中间输出 | execution + attempt | /outputs，校验后提交 |
| 正式 Artifact | WorkItem + 合同/审批版本 | 既有发布与交付账本 |
| 记忆/数据库 | 当前企业和会话作用域 | 留在企业存储，不挂给 VM |

首版小资料显式导入导出，大资料可使用已封存版本的 Cube 只读 Volume。
只读挂载本身不代表不可变内容：其他写入者仍可能改变同一卷，发布资料包必须
额外禁止写入并固定摘要。业务文件不采用整个部门或员工目录的可写共享挂载。

## 持久化与恢复

脚本在 Guest 临时盘运算，Broker 收集输出，检查路径、大小、类型和摘要，
保存到私有 S3 兼容 Content Store，再通过 ledger 发布 WorkspaceRevision。
候选输出使用 ExecutionOutput；正式 Work Artifact 继续要求来源合同和审批。

恢复保证为最后一次成功提交的 revision。进程退出码为零不等于产物已持久化。
外部动作结果不确定时进入对账，不能通过重跑脚本保证绝不重复。
首版关闭 VM 快照自动恢复，快照不代替内容、审批和交付记录。

## 选型的条件

Cube 原生 SDK、服务端、模板和 Volume 插件需锁版验证。上线要显式启用鉴权、
默认拒绝出网、配额和资源隔离；微虚机不能自动解决企业授权和凭据外发问题。
单后端让 v0.1.3 集中验证这些合同，其他沙箱保留为调研比较，不并行实现。

## 相关文档

- [企业微信数字员工架构](enterprise-wecom-architecture.md)
- [v0.1.3 架构评审与实施计划](https://github.com/sambazhu/agentseek-enterprise/blob/production/examples/enterprise_wecom_digital_employee/V0.1.3_EXECUTION_ISOLATION_PLAN.md)
- [演进路线](https://github.com/sambazhu/agentseek-enterprise/blob/production/examples/enterprise_wecom_digital_employee/ROADMAP.md)
