---
title: CSV 沙箱业务全链批准包（骨架 v1，PENDING 标注）
type: how-to
audience: [A4]
runs: no
verified_on: 2026-09-20
sources:
  - V0.1.3_SANDBOX_BUSINESS_FINAL_WINDOW_HANDOFF.md
---

# BUSINESS_E2E_APPROVAL_PACKET（.171 汇总，2026-09-20；对应 FINAL_WINDOW §1.8）

基线：网关候选 0595b47（240 passed @ 5376080）｜节点候选 5afe44c（wheel bceeb6e3…，44/2+835+业务清单沿用）｜工单 ee886a8 双端一致。**本包为准备材料，配置批准与执行授权绑定同一批准包，未获用户批准前 §2–5 不执行。**

## A. 网络与反代（.60 管理员侧——待批准）

| 项 | 值 |
| --- | --- |
| 现状（实测 2026-09-19） | `/ai-server/*` 经 test.wkzq.com.cn:9443 → **nginx 502**（存在 location、上游已死）；`/wecom/app/*` 正常转发 .171:12001；**旧上游地址与故障原因未经配置确认——PENDING_ADMIN（此前"疑指旧 :12000"仅为推测，不作事实）** |
| 拟议变更（精确） | **仅新增/重指 `/ai-server/workspace-files/` location → `http://192.10.50.171:12001`，放行 GET+POST**；保留原 `/ai-server/` 其他路径不动（不整体迁移未知使用者）；不带 body 改写、透传标准头 |
| 验证 | 变更后 `GET https://…/ai-server/workspace-files/workspace_[a-f0-9]{64}` 应返回网关下载页（合成标记文件冒烟）而非 502 |
| 恢复 | 删除该 location（或还原备份配置+reload）；网关侧路由随 MODE=disabled 重启后不再注册 |

## B. 用户下载配置（.171——待批准）

```dotenv
AGENTSEEK_WORKSPACE_DOWNLOAD_MODE=signed_link
AGENTSEEK_WORKSPACE_DOWNLOAD_BASE_URL=https://test.wkzq.com.cn:9443/ai-server/workspace-files
AGENTSEEK_WORKSPACE_DOWNLOAD_GRANTS_DIR=/var/lib/agentseek/workspace-downloads   # root(网关 UID) 0700；程序建 0600 SQLite
AGENTSEEK_WORKSPACE_DOWNLOAD_TTL_SECONDS=600
```

不使用节点私网执行地址 :13100；不静态暴露工作区；链接=短期 bearer（可转发风险已知悉入 §2 批准项）；AGENTSEEK_WORK_ARTIFACT_DELIVERY_MODE 保持 disabled。

## C. 网关切换方案（.171——待批准）

| 项 | 值 |
| --- | --- |
| 运行形态 | 单实例切换（同 LC bot 不可双实例）：候选 0595b47 examples 树 + **四根 PYTHONPATH**（examples/files/wecom/execution 候选 src）+ 生产 venv 零安装 |
| spec | `BUB_LANGCHAIN_SPEC=enterprise_wecom_digital_employee.sandbox_spec:build_spec` + `AGENTSEEK_SANDBOX_BUSINESS_CONFIG(+_SHA256)`（8 字段 approved=true 最终件，**于窗口内上传绑定核对后定稿**，不翻转待审草案） |
| 模型 | `openai:deepseek-v4-flash-0731` @ dashscope compatible-mode（真实 provider；function calling 已证） |
| runtime | 统一 examples runtime 沿用；MCP 三层/生产定制保留；WORK=false |
| 恢复 | 去 spec/config env + 标准脚本重启回生产行为；下载开关回 disabled 后路由不注册，文件与授权账本保留 |

## D. 身份与输入绑定（.171——**PENDING：待用户实际上传**）

取得方式：用户在**原生产网关同一 DM 会话**上传 `sandbox-business-input.csv`（41 字节，sha256=0ec7d232…；此步只取得文件与身份，不发执行问题）→ .171 **调用候选函数**（scoped_key/scoped_owner，不手写公式）从可信运行上下文与盘上记录取得 tenant/user/session、owner_id、input_ref（=file_id，内容派生前 16 位）、并核对输入+固定问题摘要（5941d047…）→ request_id 由 .171 铸造并两端一致钉入 grants/permits/plans。

**当前值：全部 PENDING（上传尚未发生，不猜测补齐）**。注意：重启丢 current_files；同字节 file_id 相同不代表完整授权相同——窗口内须同会话重传并核对完整绑定（FINAL_WINDOW §3.4 硬时序）。

## E. 节点方案（.172——**PENDING：待 zcode §1.2/§1.5 回传**）

11 字段核对结论、:13100 规则/恢复、CA/证书、token 交接、目录树、新 run+监督、生命周期与恢复——以 zcode 新目录回传为准汇入本包 v2。旧 run 额度/manifest/栅栏/回执保留不动；新 run 与一次创建额度另批。

## F. §2 用户一次批准清单（骨架）

网络（.60 变更+13100 规则）｜下载（B 节四项）｜网关（C 节 8 字段+grants+模型+切换恢复）｜节点（E 节 11 字段+证书/token+目录+监督限定启用）｜身份与输入（D 节完整绑定+三摘要）｜**窗口（新日期/起止，UTC+Asia/Shanghai 双写，不沿用历史）**｜创建与结束（仅一次创建+CSV 执行+正常终止+120s 监督兜底+closeout）｜W0 与预算（责任人/writer 有效确认时间关系/实际预算数值）｜文件验收（重传+提问+实际下载核验配合；链接可转发风险知悉）。

## G. 批准后硬时序（FINAL_WINDOW §3 原文约束，此处确认）

窗口内部署→合成 outbound 冒烟（不冒充真实结果）→**同会话重传相同 CSV+完整绑定核对（不一致停在创建前）**→此后至提问不再重启→预算/额度检查→用户发固定问题→真实 DeepAgent 选工具（**不用人工直调节点替代**）→程序连续执行→用户实际下载核对 376b7875…。

## H. 边界

本轮只准备材料：未签发凭据、未改网络/反代/防火墙、未启服务、未创建 guest、未切网关、未动 production/标签/R1 状态；报告不含 token/API key/完整下载链接/审批正文。
