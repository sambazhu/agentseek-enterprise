---
title: Execution contract component reference
type: reference
audience: [A2, A3]
runs: no
verified_on: 2026-09-08
sources:
  - contrib/agentseek-execution/src/agentseek_execution/models.py
  - contrib/agentseek-execution/src/agentseek_execution/authorization.py
  - contrib/agentseek-execution/src/agentseek_execution/ledger.py
  - contrib/agentseek-execution/src/agentseek_execution/service.py
---

# agentseek-execution（M1 离线参考实现）

| 模块 | 合同 |
| --- | --- |
| `models` | 不可变数据类；manifest schema 1；路径、摘要、版本与 Work/phase 成对校验 |
| `authorization` | 可信 Authority 接口；入口、提交及取消重新校验；默认无授权实现 |
| `ledger` | SQLite BEGIN IMMEDIATE；单 task 写入；lease/fencing；提交幂等；重试历史 |
| `service` | Fake 编排接缝；授权先于创建；停止确认先于内容提交 |
| `fakes` | 测试授权表、生命周期 Provider、内存内容库；不执行命令、不访问网络 |

## 使用边界

- 独立 Python 3.10–3.13 组件；无运行时第三方依赖。
- 未加入根 workspace/plugin 注册；不会被现有网关自动加载。
- 服务参数中的 actor、now、owner、模板/策略版本由可信调用方提供；不是 HTTP 请求合同。
- Ledger 为内部端口，不是可暴露给模型或业务用户的 API。
- `reconcile_stopped(confirmed=True)` 仅供可信对账器；不是用户自报停止的入口。
- SQLite 仅用于 M1 临时合成数据测试；尚无生产加密、权限初始化、迁移和保留策略。
- Fake Content Store 的摘要索引不是下载授权，也不是 S3 实现。
- media_type 为合同声明；真实类型鉴别、链接/设备文件处理、压缩炸弹及稳定导出由 M3 实现。
- 当前无真实脚本执行、VM、Broker 服务、业务文件读写或 Work 发布适配。

## 关联文档

- [M1 合同与切片](../../examples/enterprise_wecom_digital_employee/V0.1.3_M1_EXECUTION_CONTRACTS.md)
- [M1 离线复验](../../examples/enterprise_wecom_digital_employee/V0.1.3_M1_VERIFICATION.md)
