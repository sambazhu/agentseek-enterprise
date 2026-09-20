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

## D. 身份与输入绑定（.171——**已取得：用户实际 DM 上传 2026-09-20 13:25:14 UTC**）

取得方式合规：用户在原生产网关同一 DM 会话以**文件消息**上传（未发执行问题）；.171 调用候选函数（scoped_owner/instruction_digest，未手写公式）+ 盘上记录交叉验证。

| 绑定项 | 值 | 核验 |
| --- | --- | --- |
| input_ref | `file_0ec7d232fcf9e415` | 内容派生；实际字节 41B 逐字节 sha256=`0ec7d232…576e0` ✅；mime=text/csv；extract_status=done |
| tenant_key | `hmac-9b9987…934371`（前缀，全文留受控材料） | ==scoped(tenant, env TENANT_ID) ✅ |
| user_key | `hmac-81297b5…cf9d0d`（前缀） | ==scoped(employee,"zhuchunlin") ✅（**本轮上传再次实证 userid=OA 账号同串**） |
| session_key | `hmac-043001d4…cc876f`（前缀） | 与 2026-09-10 同 DM 文件会话**同键**（该会话跨日稳定）；全文留受控材料 |
| **owner_id** | `f97d455c…`（64hex，经候选 scoped_owner 计算全文入本包供 .172 permits/plans 使用） | 三键紧凑 JSON 派生 ✅ |
| **request_id**（两端一致，.171 铸造） | `4d760701569d8d4d452b4b0087b64d91654480007edc828f186e4cd58d07c31f` | 钉入 grants/permits/plans |
| instruction_sha256 | `5941d047…f96f5` | 候选 instruction_digest 复算 ==预计算 ✅ |
| 文件 expires_at | 2026-09-27T13:25:14Z（TTL 7 天） | 窗口须在此之前 |

**窗口内仍须同会话重传相同 CSV 并核对完整绑定**（重启丢 current_files；同字节 file_id 相同不代表完整授权相同——FINAL_WINDOW §3.4 硬时序不变）。

## E. 节点方案（.172——**bind 复验已闭合（BUSINESS_BIND_OFFLINE_PASS @ 66c5cd2f，a052723 归档）**）

| 项 | 值 |
| --- | --- |
| **拟部署节点版本** | **`3009d82`（schema 2，wheel `3d17d872…382ce2`）——实际安装未切换**，/opt 四版本与旧业务 venv 原样；历史 schema 1"精确 11 字段"结论保留不改写 |
| 复验证据 | 170/170 到货；新 venv-bind 17 发行版严格离线；**44/2/isolated**；**853 passed（13.83s，+18）**；业务清单 1 passed；零残留 |
| schema 2 禁用草案 | 重钉摘要 `a353954a8c0321aba520ec98db13133100ca571ec3fe31aa8821da25d8177b77`：12 字段=11+approved_bind_host；bind_host=approved_bind_host="192.10.50.172"（双地址同值，代码 :33-34 核对）；approved=false+plans=[] 双重禁用；**值级 8/8 检查 PASS**（拒 unspecified/multicast/reserved/link-local/0-8） |
| **bind 冲突已解除**（原 BLOCKED_DESIGN：192.10.50.172 非 RFC1918 私网段，schema 1 is_private 检查不过） | 方案 a 单地址显式批准；证书 SAN=IP:192.10.50.172、防火墙规则目标、服务最终件基准=**schema 2（12 字段双地址同值）** |
| 仍 PENDING（分工） | **.171**：§1.3 反代实测与方案、§1.4 下载四配置、§1.6 网关绑定（scope/owner/input_ref/request_id 已取得见 D 节——供 permits/plans 引用闭合）；**用户**：W0 新窗口+逐人确认、一次创建额度、最终批准（approved=true 另建重钉+启用窗口）；**zcode §1.2/§1.5**（final-window 回传） |
| 记录口径 | 清单 170 行 vs 工单"171 件"已按实测记录（小差异不影响判定）；README"192.10.50.50.172"笔误按 §配置变化 2 准确值执行 |

旧 run 额度/manifest/栅栏/回执保留不动；新 run 与一次创建额度另批；离线通过不自动获批——执行仍按主工单 §2 一次批准+§3 硬时序，实际部署/恢复命令由现场端在批准轮列出。

## F. §2 用户一次批准清单（骨架）

网络（.60 变更+13100 规则）｜下载（B 节四项）｜网关（C 节 8 字段+grants+模型+切换恢复）｜节点（E 节 11 字段+证书/token+目录+监督限定启用）｜身份与输入（D 节完整绑定+三摘要）｜**窗口（新日期/起止，UTC+Asia/Shanghai 双写，不沿用历史）**｜创建与结束（仅一次创建+CSV 执行+正常终止+120s 监督兜底+closeout）｜W0 与预算（责任人/writer 有效确认时间关系/实际预算数值）｜文件验收（重传+提问+实际下载核验配合；链接可转发风险知悉）。

## G. 批准后硬时序（FINAL_WINDOW §3 原文约束，此处确认）

窗口内部署→合成 outbound 冒烟（不冒充真实结果）→**同会话重传相同 CSV+完整绑定核对（不一致停在创建前）**→此后至提问不再重启→预算/额度检查→用户发固定问题→真实 DeepAgent 选工具（**不用人工直调节点替代**）→程序连续执行→用户实际下载核对 376b7875…。

## H. 边界

本轮只准备材料：未签发凭据、未改网络/反代/防火墙、未启服务、未创建 guest、未切网关、未动 production/标签/R1 状态；报告不含 token/API key/完整下载链接/审批正文。
