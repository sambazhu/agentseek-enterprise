# 业务材料生成器——脱敏交付（.172 zcode 实际使用版本）

## 如实分类

### 代码工具（可复现）
以下 10 个 Python 脚本是 .172 在多轮业务材料准备中**实际编写并运行**的生成器/验证脚本。
全部为独立脚本，仅依赖标准库 + 候选包模块。

### Agent 手工生成（不可完全复现）
部分材料文件（特别是早期 W4 轮的 approval/precreate JSON）是 Agent 在交互过程中
逐条 Python 命令/heredoc 生成的——没有保存为独立脚本文件。这些操作可从会话日志
回溯但不可精确复现。缺失部分如实标注。

### 交付候选内置生成器
m3_materialize.py（候选 13f9114 内置）——W12/W13 轮的正式材料生成器。
.172 在 W5 轮实际调用过该模块生成预批文件。

## 文件清单

| 文件 | 用途 | 轮次 | 脱敏状态 |
| --- | --- | --- | --- |
| gen_a_materials.py | A 槽审批材料（request/approval/W0/matrix） | W4 | 路径+变量名，无凭据正文 |
| gen_chain.py | 创建链材料（precreate/inst/launcher/lifecycle/biz/permits） | W4 | 同上 |
| gen_final_materials.py | 最终批准材料（approval-FINAL/W0-FINAL） | W4 | 同上 |
| gen_w2_materials.py | W2 材料（request/precreate/approval/lifecycle/matrix） | W6-W7 | 同上 |
| prep_final.py | 最终预批件生成+全链核验 | W7 | 同上 |
| materialize_key.py | api_key 物化（从受控源到 config/api_key） | W4 | 引用路径变量名，无 key 正文 |
| real_gate.py | 真实时钟门禁（W0/approval/supervisor/platform） | W4 | 无凭据 |
| site_verify.py | 现场只读核验（guest/模板/心跳/身份） | W4-W9 | 无凭据 |
| conflict_check.py | 冲突证据核对（key 副本扫描） | W4 | 引用路径变量名 |
| fix_key.py | api_key 换行修复（一次性） | W4 | 无凭据 |

## 脱敏声明

- 所有脚本仅引用**路径变量名**（如 `/root/cube-one-click-v0.7.0/poc-install.env`）和
  **目标路径**（如 `…/config/api_key`），不含任何凭据正文
- SHA256 摘要值在部分脚本中作为输出打印（如 materialize_key.py 的输出摘要），
  但这些是**摘要**而非凭据正文
- 无硬编码密码/API key/私钥/bearer token

## 不可复现部分（如实标注）

W4 轮（agentseek-m3-r1-20260917）的以下文件由 Agent 交互过程逐条命令生成，
无独立脚本可交付：
- approval-A-PENDING.json / approval-A-FINAL.json（W4 轮）
- W0-window-PENDING.json / W0-window-FINAL.json（W4 轮）
- precreate-A-PENDING.json（W4 轮）
- materialize-preapproval-A-*.json（W4-W7 轮的模板/最终件）

这些文件的结构可从 gen_a_materials.py / gen_final_materials.py 的代码逻辑推导，
但当时的具体 JSON 内容（含当时的 request_id/token/expires）不可精确复现。
