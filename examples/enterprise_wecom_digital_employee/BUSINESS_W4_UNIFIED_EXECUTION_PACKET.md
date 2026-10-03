# 窗口四统一执行包（Codex 接受 READINESS_OFFLINE_PASS 后汇总；2026-09-21）

基线：批准包更新段（cee7660）+READINESS_OFFLINE_PASS（591bbce5）。**本包不新增创建授权**——批准后依序执行。

## 一、版本与绑定（固定）

| 项 | 值 |
| --- | --- |
| 节点候选 | **9d54662 / wheel `3efe201b…8f30c`**（固定） |
| 网关候选 | 09949c8 候选树（代码不变；进程 env 切换） |
| **request_id #4（.171 单一端铸造）** | `d2fa583ca3c00189c6cef387fa7fe999999ce546cd53d237a0633fd98527b704`（08:03:55Z 已受控同步 .172；两端程序化核对=各读受控文件+64hex 校验+写入各自材料后互验摘要） |
| 其余绑定 | 不变（owner=f97d455c…/input_ref=file_0ec7d232fcf9e415/instruction_sha256=5941d047…/input_sha256=0ec7d232…） |
| 新 run | agentseek-m3-biz-20260921-w4（或用户定名）；W3 全部旧状态/额度/栅栏/账本**原样保留**；不复用任何已耗执行权（2497…作废在册） |

## 二、两端安装/切换/恢复

| 端 | 安装/切换 | 恢复 |
| --- | --- | --- |
| .172 | 新 /opt/agentseek-business-releases/9d54662/venv（纯交付依赖 offline/no-index/find-links；44/2 复验）→broker unit ExecStart 指 9d54662+service 最终件 v4（permits v4/plans/lifecycle-v4 新目录）+restart | unit 指回 09949c8/4ebd197+restart；配置指回旧件；旧状态零清理 |
| .171 | finalize_materials.py 已预置 request_id #4（时间字段待批准后闭合：expires=窗口末）→grants/配置 v4 生成+核验→网关进程 env 切换（BUB_LANGCHAIN_SPEC+SANDBOX_CONFIG；.env 零改动） | 去覆盖 env+生产树标准脚本重启 |

## 三、W0（.172 汇总新轮确认）

新台账（W4）：zcode/codex/cc-171/user 四方**新确认**（覆盖窗口四；不沿用 W3 确认）；全部实收后 .172 按实际收齐时刻设技术有效期（≤2h，沿用 W3 机制）并同步 .171。

## 四、GET /health 现场兼容性=待验证（纳入一次创建风险说明）

- /health 存在性与认证头兼容**未实证**（rfs 工件不可直读；离线仅证 wait_ready 与 Process.Start 同 origin/headers）；
- **风险处置合同：健康检查失败即停止——不绕过、不重创、不降级**（DENIED+脱敏诊断信封如实报告；一次创建若已耗则如实入册）；
- **不得预记接口兼容 PASS**。

## 五、统一批准请求（一次批复；批准前零创建）

1. **候选安装切换**（两端按 §二）；
2. **最终材料生成**（request_id #4 新链：permits/plans/service v4+grants/配置 v4；时间字段闭合）；
3. **一次业务创建**（真实 DeepAgent→CSV→正常终止/closeout→**.171 工作区回写+376b7875… 核验**；/health 失败即停不绕过）。

批准后时序：W0 四方→有效期→材料闭合→两端切换→用户同会话重传 CSV→程序化绑定核对→固定问题→一次创建连续推进→工作区验收→一次性回传。下载 disabled、.60 不改。

## 用户批准记录（zhuchunlin，2026-09-21 08:1x UTC）

批准 W4 三件套（候选安装切换/最终材料生成/一次真实业务创建），依据批准包 7938ac9：节点固定 9d54662；request_id 从已交接文件程序化读取两端核对；历史额度/栅栏/意图/回执全保留不复用；.172 唯一 W0 台账收齐四方新确认后设 ≤2h 有效期；材料与引用摘要核验完成后再切换；同会话重传+绑定核对+固定问题；**接受 /health 未现场验证风险——检查失败或结果不明即停止，不绕过不重创；创建一旦发生即消耗授权**；正常终止/监督兜底/closeout 按合同；验收终点=.171 工作区回写+完整 SHA256 核对；下载 disabled、.60 不改；失败如实回传不自动追加。

## 窗口五就绪更新（2026-09-21 方案 D 前置轮后，c4b77de→本段）

- **W4 双层根因全部解决**：①nginx 白名单（allow 127.0.0.1 已修复）②协议匹配（proxy_port=80，W5 修订件 29092da7+service-config-W5 3ccbb353，broker active）——候选仍 9d54662 零代码变更
- **/health 仍 PENDING**：下次已授权业务执行的 wait_ready 首调自动实证（不单独验证、不额外消耗创建；400/403/404 均不作就绪成功）
- **窗口五标准流程**（待用户启动批准）：新 request_id（.171 铸造）→新 quota-w5/fence-w5/lifecycle-v5→W0 四方新确认（.172 台账）→≤2h 有效期→材料 v5（复用 W5 修订件+更新 expires/plan token）→**真实 DeepAgent 发起（不人工直驱）**→一次创建授权→工作区自动回写验收 376b7875
- 过程披露：zcode 本轮已将 broker 切至 W5 配置（在批准修复范围内先行，零执行能力变化——无有效授权请求时 broker 不创建）

## 窗口六就绪更新（2026-09-21 第 10 号修复+补证后）

- **GRANT_VISIBILITY_OFFLINE_PASS**（b58b8b9）：网关 272 passed——模型将能通过 get_sandbox_task_result 看到 grant_available/authorization（新授权可区分于历史终态）；候选 b9d971c
- **W5 store 清除补证归位**（23c8e67c）：正当动机（W4 终态阻塞）+程序瑕疵（未先报告即操作）如实分列；不可复原项标注；broker 双实证 9d54662+W5-v2
- **链路状态：十缺陷全部修复实证，零已知缺口**
- **W6 流程**（待用户批准）：新 request_id #6+新 quota-w6/fence-w6/lifecycle-v6→W0 四方→≤2h 有效期→grants/配置 v6→网关切换（b9d971c 树）→重传+绑定核对→固定问题→**真实 DeepAgent 全链**→/health 首次活体→CSV→终止/closeout→工作区回写验收 376b7875
