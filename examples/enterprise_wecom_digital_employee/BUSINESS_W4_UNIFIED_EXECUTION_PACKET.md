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
