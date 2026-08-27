---
title: 数字员工研发分支、验证与合并流程
type: how-to
audience: [A1, A2, A3]
runs: yes
verified_on: 2026-07-23
sources:
  - ../../Makefile
  - ../../tests/cli_commands/test_templates_render.py
  - .gitignore
  - V0.1.1_DEPARTMENT_DIGITAL_EMPLOYEE_PLAN.md
  - AGENTS.md
---

# 数字员工研发分支、验证与合并流程

本文规定研发人员如何从 GA 基线创建分支、开发、验证、提交，并把 commit 交给
维护者合并。研发人员不要直接修改 `production` 或重写 GA tag。

## 1. 准备仓库

检查远端名称：

```bash
git remote -v
```

同步当前生产分支：

```bash title="not executed in this run"
git fetch --all --prune
git switch production
git pull --ff-only
```

这些命令会访问远端并改变当前分支，本次未执行；已在现有 `production` 工作区
验证远端配置和 GA ancestor。

确认 GA 是基线祖先：

```bash
git merge-base --is-ancestor enterprise-wecom-v0.1.1-ga production
```

命令退出码应为 0。

## 2. 创建研发分支

建议命名：

```text
enterprise/<version>-<short-topic>
```

例如：

```bash title="not executed in this run"
git switch -c enterprise/v0.1.2-department-briefing
```

这是分支命名示例，本次文档改动保留在当前工作分支，没有额外创建分支。

一个分支只解决一个清晰主题。不要把无关重构、环境配置和业务功能混在一起。

## 3. 开发前记录边界

在 issue、设计文档或合并请求中先写：

- 目标员工和业务问题；
- 是 Skill、MCP、Playbook、Plugin 还是 Profile 改动；
- 本轮明确不做什么；
- 是否涉及 schema、Pack、Profile 或 Playbook 版本；
- 权限、员工确认和数据范围；
- 验收消息与账本 oracle；
- 回滚方法。

正式 Playbook 先定义合同和状态边界，再写模型提示。

## 4. 保护本机配置

以下文件不得提交：

```text
.env
.agents/mcp.local.json
.venv/
runtime/*
models/
本机 JDBC 凭据或私有 jar
下载文件、token、grant
smoke override
```

提交前检查：

```bash
git status --short
git diff --check
```

不要恢复或覆盖其他研发人员尚未提交的本机改动。不要用 `git reset --hard` 清理
工作区。

## 5. 运行分层测试

先运行受影响模块的定向测试，再运行完整门禁。

### 示例和 Work

```bash
make check-work
```

```bash
PYTHONPATH=examples/enterprise_wecom_digital_employee/src \
  uv run pytest examples/enterprise_wecom_digital_employee/tests -q
```

### 企业、企微、文件和模型适配

```bash
make test-enterprise
make test-wecom
```

```bash
uv run pytest \
  contrib/agentseek-files/tests \
  contrib/agentseek-langchain/tests -q
```

### 类型和格式

```bash
PYTHONPATH=examples/enterprise_wecom_digital_employee/src \
  uv run ty check \
  examples/enterprise_wecom_digital_employee/src \
  examples/enterprise_wecom_digital_employee/tests
```

```bash
uv run ruff check --no-fix \
  examples/enterprise_wecom_digital_employee/src \
  examples/enterprise_wecom_digital_employee/tests
```

### 模板和文档

改动 enterprise-wecom 示例或模板时运行：

```bash
PYTHONPATH=. uv run pytest \
  tests/cli_commands/test_templates_render.py \
  -k "enterprise and wecom" -q
```

```bash
make docs-test
```

不要把测试通过数量写成永久门槛；用退出码和当前分支测试清单判断，因为用例数量
会随功能增加。

## 6. 检查受保护边界

如果任务不包含企业 MCP policy 变更，确认受保护文件相对 GA 零差异：

```bash
git diff enterprise-wecom-v0.1.1-ga -- \
  contrib/agentseek-enterprise/src/agentseek_enterprise/mcp_policy.py \
  examples/enterprise_wecom_digital_employee/src/enterprise_wecom_digital_employee/tools.py
```

预期无输出。

涉及 Pack 时确认：

- Skill SHA 与内容一致；
- `pack.yaml` 和 `profile.yaml` 的 pack version 一致；
- Profile/Skill/Playbook 引用完整；
- 新 PackSnapshot 可注册；
- 旧快照保留；
- 没有 immutable conflict。

涉及 schema 时确认：

- 新数据库可从 revision 1 正向迁移；
- 既有数据库可从上一 revision 升级；
- 旧行仍可读取；
- 没有直接业务数据 `DELETE`/`UPDATE` 脚本。

## 7. 做活体验证

先用隔离环境和临时 smoke override。不要提交 smoke 修改。

最低验证：

1. “你是谁 / 你能做什么 / 怎么使用你 / 我是谁”；
2. 普通对话和统一能力池；
3. 路由 selected / clarification / out-of-scope；
4. 正式服务的正向完整流程；
5. 错版本、否定、疑问、未授权、stale 等负向流程；
6. 重放幂等和并发；
7. Gateway 重启恢复；
8. burst、背压、TTL 和 exactly-once；
9. 事件、MCP audit 和业务账本分离；
10. 无 Traceback、IntegrityError、凭据、PII、宿主路径和内部 marker 泄漏。

活体验证文档只记录必要 ID 的缩略值和 digest，不记录 token、OA 明文或数据库密码。

## 8. 提交代码

提交前再次检查：

```bash
git status --short
git diff --check
git diff --stat
```

使用能说明意图的 commit：

```bash title="not executed in this run"
git add <changed-files>
git commit -m "feat(enterprise-wecom): add department briefing playbook"
```

命令含待开发文件占位符，本次未执行。

常用前缀：

- `feat`：新能力；
- `fix`：缺陷修复；
- `docs`：仅文档；
- `test`：测试；
- `refactor`：不改变行为的整理；
- `chore`：构建和维护。

不要把 `.env`、runtime、模型和验证下载文件加入暂存区。

## 9. 双端推送研发分支

本项目要求所有发布分支、`production` 和正式 tag 同时推送到
公司 GitLab 和 GitHub。GitHub 是家庭等公司网外环境的必备回退源，
不再视为可选镜像。以实际远端名为准：

```bash title="not executed in this run"
git push -u company-gitlab enterprise/v0.1.2-department-briefing
git ls-remote company-gitlab \
  refs/heads/enterprise/v0.1.2-department-briefing
```

```bash title="not executed in this run"
git push -u origin enterprise/v0.1.2-department-briefing
git ls-remote origin \
  refs/heads/enterprise/v0.1.2-department-briefing
```

两次 `git ls-remote` 必须返回同一 commit。任一端推送失败时，
交付状态必须标记为“双端未同步”，不得宣布发布完成。

## 10. 提交给维护者合并

发送以下信息，不要只发一句“已经完成”：

```text
主题：
分支：
代码 commit：
基线 commit/tag：

目标：
明确不做：

变更：
- Profile/Pack/Skill/Playbook/MCP/schema
- 关键文件

验证：
- 静态测试
- 活体正向
- 活体负向
- 幂等/并发/重启
- 健康和脱敏

版本：
- schema revision
- Pack/Profile/Skill/Playbook version

风险和回滚：
- 已知非阻断项
- 回滚到哪个 commit/tag

请维护者：
1. 复核 commit；
2. 在 Mac mini/准生产环境拉取验证；
3. 通过后合入 production；
4. 由维护者决定是否打 GA tag。
```

维护者需要能从 commit 重现结果。临时环境覆盖应单独说明，但不能提交。

## 11. Mac mini 复验交接

给验证机的提示词至少包含：

- 分支和精确 commit；
- commit 的 ancestor 关系；
- 禁止覆盖的本机配置；
- 静态命令；
- schema、PackSnapshot 和 MCP zero-diff 预期；
- 每条 live 消息和数据库 oracle；
- PASS/BLOCKED 判定条件；
- 验证文档位置；
- 只提交验证文档；
- 需要同步的远端和 `ls-remote` 自检。

发现阻断时：

- 只提交验证文档，不顺手改生产代码；
- 给出复现消息、时间、work/version/digest 和代码位置；
- 明确哪些已经 PASS，无需整轮重测；
- 不直接删除 WorkItem 或手工更新状态。

## 12. 合并和发布权限

研发人员默认只推送功能分支。以下动作由维护者执行：

- 合并到 `production`；
- 创建或移动正式 tag；
- 发布 Release；
- 确认 GitHub/GitLab 的发布 ref 已同步到同一 commit；
- 更新生产部署；
- 宣布 schema/Pack/Profile 冻结。

GA tag 一经发布应保持不可变。后续修复使用新 commit 和新版本，不重写历史。
