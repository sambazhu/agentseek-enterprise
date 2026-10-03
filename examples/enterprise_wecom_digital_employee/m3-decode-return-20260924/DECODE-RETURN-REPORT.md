# DECODE 轮离线复验回传（zcode-172 → cc-171）

判定：**DECODE_OFFLINE_PASS**（仅离线复验；不声称 W14 唯一根因已确定，不声称真实 CSV 已通过）

## 一、交付核验
- DECODE-MANIFEST.sha256：清单条目 270，实际校验 270，OK=270 / FAILED=0（条目数与包文件总数分列，未混用）
- wheel：agentseek_execution_business-0.1.3a0-py3-none-any.whl
  SHA256=a2d97aa0355f4f469840da3c2c0e9562a3ed88af82bec83877e4eb1ad9a27620（与预期一致）
  成员 49 = 45 个 .py（含 1 个 __init__.py）+ 4 个 dist-info，无其他成员
- package/ 与 source-closure/ 未混用（各测试接线见下）

## 二、环境（全新独立，未触碰现役安装）
- scratch：/root/m3-decode-scratch-20260924/（venv 新建于其中）
- DECODE_PYTHON=/root/m3-decode-scratch-20260924/venv/bin/python
  （由已批准解释器 /opt/agentseek-m3-supervisor/python/cpython-3.13.14-linux-x86_64-gnu/bin/python3.13 创建，Python 3.13.14）
- 安装命令全文：
  pip install --no-index --find-links <D>/deps --find-links <D>/wheel <D>/wheel/agentseek_execution_business-0.1.3a0-py3-none-any.whl pytest
- 异常处置①：pip 26.1.2 无 --offline 选项（报 "no such option: --offline"，命令全文已留）。
  离线约束由 --no-index --find-links 完整承担；未联网（全部成功解析自本地链接目录）。
- 安装发行版（16+pip）：agentseek-execution-business==0.1.3a0, anyio==4.15.1, certifi==2026.7.22,
  cffi==2.1.0, cryptography==49.0.0, h11==0.16.0, httpcore==1.0.9, httpx==0.28.1, idna==3.19,
  iniconfig==2.3.0, packaging==26.3, pluggy==1.6.0, pycparser==3.0, Pygments==2.21.0,
  pytest==9.1.0, typing_extensions==4.16.0

## 三、四组实测
1. 安装检查（tools/m3_r1_install_check.py + package/R1_INVENTORY.json --profile business）：
   isolated=true，installed_modules_verified=45，invalid_input_rejected=2，exit 0
2. 业务闭包（cwd=source-closure，-o pythonpath=examples/enterprise_wecom_digital_employee 单值）：
   contrib/agentseek-execution/tests + examples/enterprise_wecom_digital_employee/tests
   → **944 passed**（13.57s）。未将 contrib/agentseek-execution/src 加入 pythonpath；
   执行包来自 venv 已装 wheel（反证：-I 下 contrib 不可导入，business_service 实际来源=venv site-packages）
3. 材料工具（-o pythonpath=<D>/tools）：test_business_materials.py + test_business_materials_input.py
   → **61 passed**（1.51s）。未使用旧轮 toolvenv/旧工具
4. 子闭包清单（AGENTSEEK_BUSINESS_INVENTORY_ROOT=<D>/source-closure，
   AGENTSEEK_BUSINESS_PACKAGE_TEMPLATE=<D>/tools/business_package.toml）：
   → **1 passed**（0.43s），非完整仓库 6 项

## 四、导入来源核验（与套件同接线，两单值 pythonpath 分别断言）
- agentseek_execution / business_cube_session → venv site-packages ✓（含收集期-运行期时序核验）
- sandbox_poc → source-closure/examples 唯一来源 ✓
- business_materials / business_materials_input → 交付 tools 目录 ✓
- 反证：闭包 contrib/agentseek-execution/src 在 -I 下不可导入 ✓
- 异常处置②：pytest -o pythonpath 的条目在运行期已插入 sys.path，但测试模块顶层导入发生在
  收集期（插入前）——首版核验文件以模块级导入报 ModuleNotFoundError（非路径未生效）。
  已改为与闭包套件一致的函数内导入并按各套件单值接线拆分，3 项断言全过。
  全程未改用 PYTHONPATH，未混用 -I 与 PYTHONPATH，未加入闭包执行源码绕过。

## 五、诊断能力合成核验（合成数据，5/5 passed，附于本目录 test_diagnostics_synthetic.py）
- diagnostics_enabled 默认关（plan 缺省 seam 返回 None）；显式开启时仅附加失败结构证据
  （validate_evidence 白名单通过：substage 枚举/帧对[int,int]/字节数/布尔/exit_code 界）
- protocol 与 program 分类分离：CommandProgramError→program、CommandProtocolError→protocol、
  ValueError→protocol、ContractError→contract；end_code 三分支（冲突/带error/异形status）各归其类
- 不记录正文：emit 输出恰为 13 个标量字段白名单；error 仅类型名（注入 SECRET/凭据正文不出现）；
  白名单外 stage/event 事件被丢弃；无 stdout/stderr 正文、无十六进制前缀
- 材料生成保持禁用：合成参数 build_input 后 approval/w0/session(csv,termination)/permits/service
  批准字段全 False、allowPublicTraffic=False，产物无执行授权语义

## 六、未执行项
- 无。工单四/五/六/八全部执行；三处异常（--offline 缺失、收集期导入时序、合成用例两处构造修正）
  均已如实记录处置，未静默绕过。

## 七、边界遵守
- 未切 broker（现役仍为 W14-FINAL f027d46a…，active）；未创建 guest；未清任何旧状态；
  未调用真实 perform；旧 /opt 安装与现场配置零改动（新 venv 位于 scratch）。
