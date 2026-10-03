---
title: CSV 沙箱业务全链最终批准包（v5：节点拟部署版本=4ebd197，UNIT 阻塞解除）
type: how-to
audience: [A4]
runs: no
verified_on: 2026-09-20
sources:
  - V0.1.3_SANDBOX_BUSINESS_FINAL_WINDOW_HANDOFF.md
  - V0.1.3_SANDBOX_BIND_APPROVAL_LINUX_RESULTS.md
  - V0.1.3_SANDBOX_BUSINESS_E2E_PREPARATION.md
  - V0.1.3_SANDBOX_WORKSPACE_DOWNLOAD_LINUX_RESULTS.md
---

# BUSINESS_E2E_APPROVAL_PACKET v4（收敛版；.171 汇总，2026-09-20）

**配置批准与执行授权绑定本包；未获用户批准前不执行任何部署。本轮零执行零变更。**

## 〇、版本矩阵（全部已验收，不再追加底层矩阵轮）

| 端 | 版本 | 证据 |
| --- | --- | --- |
| 网关候选 | `0595b47`（含工作区下载） | 240 passed @ 5376080；下载代码本轮后未再变更 |
| 节点候选（拟部署） | **`4ebd197`（schema 2 + 8 字段 pins-v2），wheel `2290eaf6…ff97b`** | BUSINESS_SUPERVISOR_UNIT_OFFLINE_PASS @ 246d4f39（867/44-2，ace55f2）+ 现场只读身份核验 PASS（verify_identity×1 exit 0，pins-v2 `8c1555ac…afcd`，UNIT 硬编码阻塞解除）；前身 3009d82 bind 复验 853/值级 8/8 @ 66c5cd2f；**实际安装未切换**（/opt 各版本原样，venv-unit 仅 scratch） |
| 绑定 | 2026-09-20 13:25:14Z 用户实际 DM 上传 | D 节（保留，不重新上传） |
| 基线 | `007cbb2` | 双端一致 |

## A. 网络（.60 反代 + .172 定向规则）

**.60 管理员项（精确方案——实施/验证/恢复）**：
- 实施：仅新增/重指 location `/ai-server/workspace-files/` → `proxy_pass http://192.10.50.171:12001`；放行 **GET+POST**（redeem 为 POST，body 承载 token，勿改写 body）；透传标准头；**保留原 `/ai-server/` 其他路径与使用者不动**。
- 验证：变更后 `GET https://test.wkzq.com.cn:9443/ai-server/workspace-files/workspace_[a-f0-9]{64}` 返回网关下载页（合成标记冒烟）而非 502；`/wecom/app/*` 回归不受影响。
- 恢复：删除该 location（或还原备份+reload）。
- **旧上游 502 故障原因：不作为阻塞项**（实测事实已留档；如管理员顺手确认原因则记录，不影响本包）。

**.172 :13100 定向规则（拟议命令，待批准由 zcode 执行）**：
```
iptables -A CUBE_POC_GUARD -s 192.10.50.171/32 -p tcp --dport 13100 -j ACCEPT
iptables -A CUBE_POC_GUARD -s 127.0.0.1/32 -p tcp --dport 13100 -j ACCEPT
iptables -A CUBE_POC_GUARD -p tcp --dport 13100 -j REJECT --reject-with tcp-reset
```
恢复=对应 `-D` 三条（或按行号删）；不涉 NAT/持久化，如需 netfilter-persistent 另列批准。

## B. 用户下载配置（.171，四键——已定稿待批准启用）

```dotenv
AGENTSEEK_WORKSPACE_DOWNLOAD_MODE=signed_link
AGENTSEEK_WORKSPACE_DOWNLOAD_BASE_URL=https://test.wkzq.com.cn:9443/ai-server/workspace-files
AGENTSEEK_WORKSPACE_DOWNLOAD_GRANTS_DIR=/var/lib/agentseek/workspace-downloads
AGENTSEEK_WORKSPACE_DOWNLOAD_TTL_SECONDS=600
```
目录：网关 UID 所有 0700（程序建 0600 授权 SQLite）；不静态暴露工作区；AGENTSEEK_WORK_ARTIFACT_DELIVERY_MODE 保持 disabled。链接=短期 bearer（可转发风险已知悉，入用户决策项）。

## C. 网关切换（.171）

- 形态：单实例切换（同 LC bot 不可双实例）；四根候选 PYTHONPATH（examples/files/wecom/execution 候选 src）+ 生产 venv 零安装。
- env：`BUB_LANGCHAIN_SPEC=enterprise_wecom_digital_employee.sandbox_spec:build_spec` + `AGENTSEEK_SANDBOX_BUSINESS_CONFIG`(+`_SHA256`)（8 字段 approved=true 最终件，窗口内绑定核对后定稿）+ B 节四键。
- 模型：`openai:deepseek-v4-flash-0731` @ dashscope compatible-mode（function calling 已证）。
- 恢复：去 spec/config/下载 env + 标准脚本重启回生产行为；下载开关回 disabled 后路由不注册；文件与授权账本保留。

## D. 身份与输入绑定（.171——**已取得并保留**，2026-09-20 13:25:14Z 实际 DM 上传）

| 项 | 值 |
| --- | --- |
| input_ref | `file_0ec7d232fcf9e415`（41B 逐字节 sha256=0ec7d232…576e0 ✅） |
| tenant/user/session 三键 | 前缀 hmac-9b9987…/hmac-81297b5…/hmac-043001d4…（全文留受控材料；交叉验证 ✅；session 与 09-10 同 DM 同键） |
| owner_id | `f97d455c…`（候选 scoped_owner；全文已受控交 .172） |
| request_id（两端一致） | `4d760701569d8d4d452b4b0087b64d91654480007edc828f186e4cd58d07c31f` |
| instruction_sha256 | `5941d047…f96f5` ✅ |
| 文件 expires_at | 2026-09-27T13:25:14Z（**窗口须在此之前**；过期重传重绑） |

窗口内仍须**同会话重传相同 CSV+完整绑定逐项核对**（§3.4 硬时序，不因本值已取得而豁免）。

## E. 节点方案（.172——E2E 准备+schema 2 合并版）

| 项 | 值 |
| --- | --- |
| 业务 venv（部署） | /opt/agentseek-business-releases/4ebd197/venv（新目录；wheel 2290eaf6；44/2 复验随部署轮）——**待用户批准落位** |
| 业务根 | /var/lib/agentseek-m3-business/runs/<run_id 用户批准定名>（不复用 R1 run） |
| store/workspace | <业务根>/store/（SQLite）+ /workspace/（CsvWorkspace 0600 原子读回销毁） |
| 监督 | 新 run 专属第二监督（agentseek-m3-biz-supervisor.service **已部署 active**，MainPID 实采在册；**8 字段 pins-v2 `8c1555ac…afcd` 已落盘**（unit=完整单元名），上游创建链引用待最终材料轮逐项重生成核对；R1/M0/M2 三监督零改动） |
| 服务配置基准 | **schema 2 十二字段**（bind_host=approved_bind_host="192.10.50.172"）；禁用草案 `a353954a…77b77`；**身份 pins 已升级 v2（8 字段）**；最终件 approved=true 另建重钉 |
| 服务单元 | agentseek-business-broker.service（disabled 默认；窗口内 start 不 enable） |
| 证书 | 业务专用内部 CA agentseek-business-ca（180d）+服务器证书（90d，CN=agentseek-business-node，**SAN=IP:192.10.50.172**，TLS≥1.2）；CA 私钥留 .172；公共证书受控交 .171 |
| broker token | 64 位随机十六进制（32-256 可打印 ASCII 合同内）；.172 生成受控交 .171 0600 文件 |
| 保留 .172 | Cube API key/vault key/监督 pins/manifest（不进回传与 Git） |

## F. 两端可执行部署与恢复命令（批准后按 §3 硬时序执行）

**.171（网关）**：
```
# 1) 下载目录
install -d -m 0700 -o root /var/lib/agentseek/workspace-downloads
# 2) .env 追加 B 节四键 + 窗口内定稿 SANDBOX_BUSINESS_CONFIG 两键（8 字段文件+摘要）
# 3) 切换启动（标准脚本 envelope 外加）：
env -u PYTHONPATH \
  AGENTSEEK_LANGCHAIN_SPEC=enterprise_wecom_digital_employee.sandbox_spec:build_spec \
  PYTHONPATH=<候选树>/examples/.../src:<候选树>/contrib/agentseek-files/src:<候选树>/contrib/agentseek-wecom/src:<候选树>/contrib/agentseek-execution/src \
  AGENTSEEK_ENV_FILE=<生产 .env> AGENTSEEK_GATEWAY_LOG=<生产日志> \
  nohup bash <候选树>/examples/.../scripts/run_gateway.sh >> gateway.log 2>&1 &
# 恢复：去掉 LANGCHAIN_SPEC/PYTHONPATH 覆盖+回 .env，标准脚本重启；下载回 disabled。
```
（SANDBOX_BUSINESS_CONFIG 两键由切换命令环境或 .env 承载，以最终件摘要为准。）

**.172（节点，zcode 列并执行）**：A 节三条 iptables；业务 venv `uv pip install --offline --no-index --find-links <交付 deps> <新 wheel>`；目录树 `install -d -m 0700`（业务根/store/workspace/config/tls）；CA+证书物化；token 生成+交接；第二监督 unit+登记；服务最终件+unit 窗口内 start；恢复=规则 -D、服务 stop、unit 保留审计、旧 run 不动。**精确命令清单由 zcode 补齐回传（一次补齐项，见 §G-1）**。

## G. 动态项清单（**不得伪造；凭据生成/窗口确定后才填；不要求获批前提供其摘要**）

| # | 动态项 | 填写时机/原因 | 责任 |
| --- | --- | --- | --- |
| 1 | .172 可执行部署/恢复命令清单（精确到单元/路径） | 一次补齐项（其余节点方案已闭合） | zcode |
| 2 | broker token 值→principal_sha256=SHA256(token UTF-8) | 获批后生成 token 才能计算 | zcode 生成→.171 同步 |
| 3 | CA 公共证书/broker token 文件摘要 | 获批后物化受控交接时 | zcode→.171 |
| 4 | grants.json（scope 三键全文+input_ref+instruction_sha256+expires）及其 sha256 | 窗口确定才有 expires；随后网关 8 字段配置的 grants_sha256 才能定 | .171 |
| 5 | permits/plans 最终件（含上两项引用）+服务 approved=true 最终件 | 依赖 2/4 | zcode（引用 .171 交接值） |
| 6 | W0 责任人/窗口起止（UTC+Asia/Shanghai 双写）/预算数值 | 用户决策项 1/2 | 用户 |
| 7 | 新 run_id 命名 | 用户批准定名 | 用户→zcode |

## H. 用户决策清单（**统一批准请求——批准即覆盖以下全部**）

| # | 决策项 | 内容 |
| --- | --- | --- |
| 1 | **新窗口** | 新日期+起止时间（UTC 与 Asia/Shanghai 双写）；不沿用历史窗口；须早于 2026-09-27T13:25Z（文件 TTL）或接受重传重绑 |
| 2 | **W0 有效确认** | 责任人（建议 zhuchunlin）+writer 逐人有效确认（确认时刻须早于窗口 start——历史教训）；实际预算数值（reserve=5s 沿用或另定） |
| 3 | **限定部署变更** | .60 反代一条 location（A 节精确方案）；.172 A 节三条规则+业务 CA/证书/token+业务根目录+第二监督+业务 venv 生产安装+服务最终件+unit；.171 B 节四键+网关切换+下载目录。**仅此清单，超范围另行批准** |
| 4 | **一次创建** | 新业务 run 仅一次 guest 创建+CSV 执行+正常终止+120s 监督兜底+closeout；创建后程序连续推进无人工往返 |
| 5 | **用户提问与下载配合** | 窗口内同会话重传相同 CSV→发固定问题→真实模型选工具→执行链完成后**实际打开下载页下载 summary.csv 并核对摘要 376b7875…**；知悉下载链接为短期 bearer 可转发 |
| 6 | **批准后步骤（固定）** | ①生成最终材料（token/CA/grants/permits/两 approved=true 最终件）→②重钉全部摘要→③核验引用一致性（两端 request_id/owner_id/input_ref/三摘要逐项）→④按 §3 硬时序执行（合成冒烟→重传核对→不再重启→提问→全链→用户下载）→⑤一次性回传判定 |

## I. 边界

本轮零执行：未启服务、未创建 guest、未切实际安装、未改网络/.env/反代/防火墙；R1 状态/历史材料/production/标签全保留；动态项未伪造（G 节如实标注填写时机）；报告不含 token/API key/完整下载链接/批准正文。

## J. v5 增补（监督单元修复轮后状态，2026-09-21）

1. **已部署基础设施实况**（窗口一部署 1-6，保留待用/回退由用户裁定）：13100 三条规则生效、/opt/agentseek-business-releases/3009d82/venv（bind 轮版本，**4ebd197 落位另批**）、bizroot 目录树、业务 CA+证书（SAN=IP 实证）、broker token（.171 私有文件在位 d52e0d85/5111a50c）、第二监督 active。
2. **执行前待用户批准三步**：a) 4ebd197 安装落位（新 /opt venv+44/2 复验）；b) 最终材料生成（8 字段 pins 上游引用闭合+grants/permits/plans+服务 approved=true 最终件+网关 8 字段配置）；c) 新窗口指定（UTC+北京双注）。
3. **W0 新规**：.172 唯一台账点，全部 writer 确认（含 codex 本人）实收后再定窗口。
4. **验收终点不变**：.171 用户工作区自动回写+完整摘要 376b7875… 核对；浏览器下载 NOT RUN；不改 .60、不启下载。
5. 观察项（非阻塞）：闭包首跑 1 例瞬态失败（test_secure_ownership，复现矩阵 867×2+单文件 12×3 全过）——已交 Codex 排查测试间状态污染。
