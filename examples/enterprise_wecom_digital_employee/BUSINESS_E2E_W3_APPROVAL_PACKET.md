---
title: CSV 业务窗口三两端统一批准包
type: how-to
audience: [A4]
runs: no
verified_on: 2026-09-21
sources:
  - V0.1.3_W3_EXECUTION_PREP_AND_APPROVAL_REQUEST.md
  - BUSINESS_W3_UNIFIED_APPROVAL_REQUEST.md
---

# BUSINESS_E2E_W3_APPROVAL_PACKET（.171 汇总两端，2026-09-21；归档链 852baf5→7077aa1→0af4164→本件）

## 〇、request_id 收敛（重要——两端曾各铸一个，统一如下）

| 值 | 状态 |
| --- | --- |
| **`249714095a3842d78c722c4308567ef2287891367a6d903fd1681da13082fbf9`**（.172 拟议，已入其准备材料） | ✅ **采用**——两端 grants/permits/plans 统一绑定此值（批准后 .171 grants v3 与 .172 permits v3 程序化对齐） |
| `250880fd06a0c42ebea77789e25e5ab92fb57cadb8c561b04715a3ae540ef7d6`（.171 此前铸造） | ❌ **作废未用**（防止双值混流——窗口二 400 教训内化） |

## 一、两端拟部署版本与命令（已核对一致）

| 端 | 版本 | 切换 | 恢复 |
| --- | --- | --- | --- |
| .172 节点 | **09949c8** wheel `ab8ace00…f1dacc` 已落位 /opt/agentseek-business-releases/09949c8/venv（44/2/isolated、:160 双参修复确认、INSTALL-RECORD 2f01ed2e；4ebd197 保留回退） | broker unit ExecStart 指 09949c8+新摘要+restart；permits v3+新链+service 最终件 v3 重钉 | unit 指回 4ebd197+restart；配置指回旧件；旧账本/额度/栅栏零清理 |
| .171 网关 | **09949c8 候选树**（含 0595b47 的全部下载代码+窗口二修复：failed 文案/env 优先级——下载代码本轮未变） | **进程 env 切换**：BUB_LANGCHAIN_SPEC=…sandbox_spec:build_spec + AGENTSEEK_SANDBOX_BUSINESS_CONFIG(+_SHA256)（**新优先级下无需改 .env**）+四根候选 PYTHONPATH+生产 venv 直启 | 去覆盖 env+生产树标准脚本重启（零 .env 还原动作）；下载 disabled |

## 二、.171 侧状态填写（工单要求）

- **下载四配置**：本轮**保持 disabled 不启用**（.60 不改豁免延续）；四键草案在档（BASE_URL/目录 0700/UID root/TTL=600），启用留待未来轮次另批。
- **broker token/CA v2 已加载确认**：/etc/agentseek-sandbox-business/（0700/0600）——**CA v2 `684ffebb…19fc1`**（KeyUsage critical CertSign+CRLSign，TLS 复验 PASS）+ **broker token 摘要=principal `5111a50c…c946`**（64B 无换行自证）；仅网关配置加载用途。
- **grants 对齐状态**：finalize_materials.py 就绪——批准+窗口确定后一参生成 grants v3（**request_id=2497…fbf9**、expires=窗口末）+网关配置 v3 重钉；全部程序化构造。

## 三、绑定与额度（新 run）

owner=f97d455c…/input_ref=file_0ec7d232fcf9e415/instruction_sha256=5941d047…/input_sha256=0ec7d232…（全部不变、程序化）；**一次创建额度=新 run**（新 lifecycle 目录+新 quota 序列）；旧额度 slot-0 已耗、旧 fence/manifest/账本全保留。

## 四、W0-w3 台账（.172 唯一台账点 40d47073…）

zcode-172 ✅（1/4）；**待收：cc-171（已物理送达 .172 /root/W0-ledger-inbox-cc171-W3.md，07:01:44Z mtime，待登记）/codex-dev/user-zhuchunlin**——全部实收且 ≤start 后定窗口；不沿用旧确认。

## 五、审计事项（单列，不归入代码修复 PASS——定性权在用户）

- **A** 第二 batch 计划授权：batch[1]=结构占位（quota 无 slot-1、从未获准执行；slot 递增防越权 m3_batch_quota.py:43-56 已确认）——占位≠授权需合同化或逐窗知悉。
- **B** lifecycle→v2 换目录：系旧链含已知缺陷（artifact SHA 笔误）而非规避失败；O_EXCL 烧毁正确阻止错误链重试。

## 六、统一批准请求（一次批复三件；未经批准不创建）

1. **候选安装切换**：.172 broker 切 09949c8 ＋ .171 网关进程 env 切换。
2. **最终材料生成**：grants v3/配置 v3（.171）＋permits v3/创建链/service 最终件 v3（.172）——request_id 统一=2497…fbf9。
3. **一次业务创建**：真实 DeepAgent→CSV 沙箱执行（python3 3.12.12 法证在模板）→正常终止/closeout→**.171 用户工作区回写+摘要 `376b7875…` 核对**。下载 disabled、.60 不改。

## 七、批准后时序

批复→.172 登记 cc-171+收 codex/user 确认→四方实收齐→**用户指定窗口**（UTC+北京双注；start≥最晚实收）→窗口内：.172 切换+材料→.171 网关切换→用户同会话重传 CSV→程序化绑定核对→固定问题→一次创建连续执行→工作区验收→一次性回传判定。

## 更新（2026-09-21 readiness 轮后——节点拟部署版本刷新）

| 项 | 值 |
| --- | --- |
| **节点拟部署版本** | **`9d54662` / wheel `3efe201b…8f30c`**（READINESS_OFFLINE_PASS @ 591bbce5：882/44-2/六项覆盖逐一确认；第十一次确定性构建） |
| readiness 修复 | 经 .172 Linux 独立复验通过，代码侧无阻塞 |
| GET /health 存在性 | **PENDING**——将在下次执行时 wait_ready 首次调用自动实证（200/204→继续；非白名单→DENIED+脱敏诊断信封；非阻塞项） |
| .172 现场 | broker active（09949c8，待切 9d54662）+监督 active+guest=0+旧状态全保留 |

**重要精确化（.171 补注）**：request `2497…fbf9` 的执行权**已随 W3 终态耗尽**（attempt 8c22bcfe=failed terminal、closeout next_create_authorized=false；broker 对其重复 execute 只读返回旧终态）。**下次执行（窗口四）需要：新 run + 新 request_id（两端重新程序化对齐）+ 新 permits/plans/grants/lifecycle 材料 + 新一次创建授权**——不得沿用 2497 或 run 20260921 的额度。网关侧流程全部现成（生成器/切换/绑定核对）。

**窗口四执行序**（批准后）：新 request_id 铸造与两端对齐→.172 落位 9d54662+材料 v4→.171 grants/配置 v4+网关切换→W0 四方新确认→用户重传 CSV→绑定核对→固定问题→一次真实全链（wait_ready 活体实证）→工作区回写验收 `376b7875…`。
