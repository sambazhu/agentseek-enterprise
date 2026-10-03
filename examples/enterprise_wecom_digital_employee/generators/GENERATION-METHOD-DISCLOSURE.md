# W10–W13 业务材料生成方式：无可复现生成器（如实披露）

## 结论（工单要求直接说明）

**W10–W13 的 business-session、permits、service-config、创建链（approval/precreate/create-installation/launcher/lifecycle）及 unit 摘要引用，全部由 zcode Agent 在对话中临时手工构造 JSON 并直接写入磁盘，没有保存可复现的生成脚本。**

不使用早期 R1 脚本或 m3_materialize 代替——它们服务于不同的合同结构。

## 逐项分类

| 文件类别 | 生成方式 | 可复现性 |
| --- | --- | --- |
| permits-W10~W13.json | Agent 手工写 JSON | **不可复现**（无脚本） |
| service-config-W10~W13-FINAL.json | Agent 手工写 JSON | **不可复现** |
| business-session-W10~W12.json | Agent 手工写 JSON | **不可复现** |
| approval-W10/W12-FINAL.json | Agent 手工写 JSON | **不可复现** |
| precreate-W10/W12.json | Agent 手工写 JSON | **不可复现** |
| create-installation-W10/W12.json | Agent 手工写 JSON | **不可复现** |
| launcher-W10~W12.json | Agent 手工写 JSON | **不可复现** |
| lifecycle-W10~W12.json | Agent 手工写 JSON | **不可复现** |
| W0-window-FINAL-W10/W12.json | Agent 手工写 JSON | **不可复现** |
| request-W10/W12.json | Agent 手工写 JSON | **不可复现** |
| api_key 物化 | materialize_key.py（已交付） | 可复现 |
| 冲突检查 | conflict_check.py（已交付） | 可复现 |
| 早期 W2~W7/A 槽 | gen_*.py（已交付） | 可复现（但合同结构已变） |

## 每个预期摘要的来源

**全部为"生成时从生成内容现算"**——即 Agent 写完 JSON 后用 sha256sum 计算摘要并填入上游引用。**没有任何独立来源的预期摘要**（如审批方预提供的摘要清单）。

这正是"api_key 两次遗漏"的系统性根源：
1. Agent 写 JSON 时凭记忆/上下文引用文件路径→遗漏物化步骤→路径指向不存在的文件
2. 摘要从生成内容现算→即使文件缺失，"生成时"的摘要链仍然自洽→"10/10 PASS"通过
3. 只有到运行时 worker 真正读取文件才发现缺失→exit 2

## 对 Codex 统一生成器的需求（从两次缺陷逆推）

1. **输入合同**：一份包含全部绑定值（request_id/owner/input_ref/instruction_sha256/api_key path/ca path/receipt_key path/supervisor pins/expires 等）的**合成输入文件**（不含凭据正文，只含路径）
2. **生成动作**：按精确字段集写 JSON+**同步物化所有引用的密钥/证书文件**
3. **生成后读回自检**：读回每枚 *_file 路径→存在+权限+sha256 对算→跨文件引用一致→缺预期摘要报"未覆盖"→失败非零退出不 READY
4. **摘要来源合同**：api_key/ca 等物化文件的摘要应在物化时从**受控源文件**计算并写入配置（而非生成后从输出倒算）

## README 勘正

已交付的 README-GENERATORS.md 声称涵盖"全部生成器"不准确——W10-W13 实际无可复现生成器（本文件为准）。原 README 保留不改写。
