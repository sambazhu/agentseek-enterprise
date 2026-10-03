---
title: 窗口三统一批准请求（运行候选 09949c8 / 归档基线 545d2e2）
type: how-to
audience: [A4]
runs: no
verified_on: 2026-09-21
sources:
  - V0.1.3_SANDBOX_WINDOW2_FIX_HANDOFF.md
  - V0.1.3_W2FORENSIC_READ_ONLY_EVIDENCE.md
---

# 窗口三统一批准请求（.171 汇总，2026-09-21）

**未经批准不创建。** 下载保持 disabled、.60 不改、验收终点=.171 用户工作区回写+完整摘要核对。

## 一、两端拟部署版本与命令（已核对）

| 端 | 项 | 值 |
| --- | --- |
| .171 网关 | 候选 | `09949c8`（树已就位；四根 PYTHONPATH+生产 venv 直启） |
| | 切换命令 | 进程 env `BUB_LANGCHAIN_SPEC=…sandbox_spec:build_spec` + `AGENTSEEK_SANDBOX_BUSINESS_CONFIG(+_SHA256)`——**09949c8 新优先级（进程 BUB>AGENTSEEK>dotenv）下无需再改 .env**（窗口二教训已修） |
| | 恢复 | 去覆盖 env+标准脚本重启（无 .env 改动需还原）；下载 disabled 不变 |
| .172 节点 | 候选 | `09949c8` wheel `ab8ace00…f1dacc`（872/44-2/1 双端复验一致） |
| | 安装切换 | **新目录** /opt/agentseek-business-releases/09949c8/venv（旧 4ebd197/3009d82 全保留；broker unit --sha256 与 plans 引用随之重钉——由 zcode 执行） |
| | 恢复 | unit 停用+指回旧版本路径；旧账本/额度/栅栏零清理 |

## 二、新绑定与一次创建（待批准后生效）

| 项 | 值 |
| --- | --- |
| **request_id #3** | `250880fd06a0c42ebea77789e25e5ab92fb57cadb8c561b04715a3ae540ef7d6`（.171 铸造，两端一致钉入 grants/permits/plans） |
| 其余绑定 | 不变（owner=f97d455c…/input_ref=file_0ec7d232fcf9e415/instruction_sha256=5941d047…/input_sha256=0ec7d232…；**全部程序化构造禁手抄**） |
| 一次创建额度 | **新 run**（新 lifecycle 目录+新 quota 序列）；旧额度 slot-0 已耗保留在册、旧 fence/manifest 原样 |
| grants/配置 | 窗口确定后由 finalize_materials.py 一参闭合（expires=窗口末）；grants_sha256→网关配置 v3 重钉 |
| 镜像绑定 | 模板 tpl-c4ba4bf8… 不变（python 3.12.12 经法证确认在模板；修复版命令面=`python3 -I -c` 经 CubeProxy 49983→49999） |

## 三、W0 确认（.172 唯一台账点，收齐后回传，暂不定 start）

- **cc-171 已送达**（实收 2026-09-21 07:01:44Z mtime 证据，digest 80425f3d…，覆盖"用户指定的下一窗口"）
- 待收：zcode-172 本人、codex-dev 本人、user-zhuchunlin（随窗口指定）——**全部须实收早于新窗口 start；不沿用旧确认**

## 四、审计事项（单列，不归入代码修复 PASS——852baf5 已入档）

1. 第二 batch 计划（结构占位）的授权语义：占位≠授权需合同化或用户逐窗知悉
2. lifecycle 换目录操作模式：换前是否须先闭合旧 intent（窗口二为绕 O_EXCL 直接换 v2）

## 五、统一批准请求（一次批准覆盖三件）

| # | 批准项 | 内容 |
| --- | --- | --- |
| 1 | **候选安装切换** | .172 落位 09949c8（新 /opt venv+broker 重钉重启）；.171 网关进程 env 切换（无 .env 改动） |
| 2 | **最终材料生成** | grants v3/网关配置 v3（request_id #3）/节点 permits-plans-服务最终件 v3（新建重钉；旧件保留） |
| 3 | **一次业务创建** | 新 run 仅一次 guest 创建→真实 DeepAgent 选工具→CSV 执行（python3 经法证应在）→正常终止/closeout→**.171 工作区回写+摘要 376b7875… 核对** |

**窗口起止由用户在四方确认收齐后指定**（UTC+北京双注；start≥全部实收）。硬时序沿用：合成冒烟不需要（下载豁免）→用户同会话重传 CSV→完整绑定程序化核对→固定问题→创建后程序连续推进→工作区验收→一次性回传。
