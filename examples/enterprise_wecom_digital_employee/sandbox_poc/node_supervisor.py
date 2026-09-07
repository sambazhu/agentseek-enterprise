"""PoC 节点侧硬截止监督（.172 侧）——v2，按 Codex 四次复核（F2–F5）修复。

合同（V0.1.3_M0_INSTALL_CHANGE_LIST.md §9.2）：
- 单 VM 硬截止 120 秒：到时即终止；宽限 30 秒仅用于终止确认；
- 时间基准统一为 epoch（time.time()）；平台 startedAt（ISO）换算 epoch；
  时钟回拨仅告警不误判（deadline 只会被推迟，不会提前杀）；
- 资源归属（不可混淆）：一级 = create 时随请求携带的
  ``metadata={"agentseek_run_id": <run_id>}``（v0.7.0 原生字段，响应丢失也
  已在服务端留痕，经 get_info 读回）；二级 = manifest 登记的精确 sandbox ID；
  末级兜底 = templateID == 本轮模板 且 startedAt ≥ 本轮开始时间（时间窗
  限界，防跨轮误杀）——仅当 marker 与登记都缺失时使用并告警；
- 创建时间不可得（字段缺失/解析失败）时：不得以 0 参与判断，告警
  "无法保证终止"，绝不静默删除；
- 异常韧性：list/kill 单点异常不退出循环；kill 404 视为已确认消失；
  确认宽限超时 → 告警文件（存在即阻止 PoC 客户端发起新建）；
  控制面不可达 → 明确报告"无法保证终止"，不宣称硬截止完成；
- 请求时延有界（Config.request_timeout），最坏执行偏差 = 轮询间隔 +
  请求时延上限（非仅 2 秒）；
- manifest：flock 读-改-写 + 临时文件原子替换 + fsync；每轮 reload
  （跨进程登记即时可见）；register_run 重置旧登记；损坏时沿用最后已知
  状态并告警，不扩大删除范围；
- 独立性：独立于 .171 客户端与被 systemd 限额的服务；与 .172 整机
  同故障域。

真实 SDK 适配（v0.7.0 源码核对）：Sandbox.list() → list[dict]
（sandboxID/templateID/state）；Sandbox(data: dict, config) 构造；
get_info() → started_at(datetime)/metadata；kill() → DELETE，404 抛
SandboxNotFoundError。适配测试（test_sdk_adapter.py）用真实 SDK +
模拟 HTTP 服务验证，不只用 FakeClient。
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import logging
import os
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Protocol

LOG = logging.getLogger("cube_poc_supervisor")

DEADLINE_SECONDS = 120  # 单 VM 硬截止（冻结值）
POLL_INTERVAL_SECONDS = 2  # 轮询间隔
CONFIRM_WINDOW_SECONDS = 30  # 终止确认宽限（仅确认，不执行新命令）
REQUEST_TIMEOUT_SECONDS = 10  # 单次控制面请求时延上限（最坏偏差组成）
RUN_MARKER_KEY = "agentseek_run_id"  # create metadata 随请求携带的本轮标识
TERMINAL_STATES = ("terminated", "removed")


class NotFoundError(Exception):
    """目标已不存在（kill 404 等）——视为已确认消失。"""


class ManifestError(Exception):
    """manifest 损坏——沿用最后已知状态并告警，绝不扩大删除范围。"""


@dataclass
class SandboxRecord:
    sandbox_id: str
    template_id: str
    state: str
    started_at: float | None = None  # epoch；None=未知，不得以 0 参与判断
    run_marker: str | None = None  # metadata 中的本轮标识


class SandboxClient(Protocol):
    def list(self) -> list[SandboxRecord]: ...

    def hydrate(self, record: SandboxRecord) -> SandboxRecord:
        """补齐 started_at / run_marker（经单查接口；404 抛 NotFoundError）。"""

    def kill(self, sandbox_id: str) -> None:
        """终止；目标已不存在时抛 NotFoundError。"""


class ManifestStore:
    """每轮一个 JSON 文件；flock + 原子替换；每轮 reload。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.data: dict = {}
        self.reload()

    def reload(self) -> None:
        try:
            with open(self.path) as fh:
                data = json.load(fh)
        except FileNotFoundError:
            self.data = {}
            return
        except OSError as exc:
            raise ManifestError(str(exc)) from exc
        except json.JSONDecodeError as exc:
            raise ManifestError(str(exc)) from exc
        if not isinstance(data, dict):
            error = ManifestError("manifest root not object")
            raise error from TypeError(data)
        self.data = data

    def _flush_locked(self, fh) -> None:
        fh.seek(0)
        fh.truncate()
        json.dump(self.data, fh, indent=2)
        fh.flush()
        os.fsync(fh.fileno())

    def _update(self, *, fresh: bool = False) -> None:
        """flock 写入。fresh=True 整体覆盖（新轮重置）；否则与磁盘基线合并
        （跨进程登记不丢失）。"""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a+") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                if not fresh:
                    fh.seek(0)
                    raw = fh.read().strip()
                    base = json.loads(raw) if raw else {}
                    self.data = {
                        **base,
                        **self.data,
                        "sandboxes": {
                            **base.get("sandboxes", {}),
                            **self.data.get("sandboxes", {}),
                        },
                    }
                self._flush_locked(fh)
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)

    def register_run(
        self,
        run_id: str,
        template_alias: str,
        template_id: str,
        *,
        started_at: float | None = None,
    ) -> None:
        self.data = {
            "run_id": run_id,
            "template_alias": template_alias,
            "template_id": template_id,
            "run_started_at": started_at if started_at is not None else time.time(),
            "sandboxes": {},  # 新轮重置旧登记
        }
        self._update(fresh=True)

    def register_sandbox(self, sandbox_id: str) -> None:
        sandboxes = dict(self.data.get("sandboxes", {}))
        if sandbox_id not in sandboxes:
            sandboxes[sandbox_id] = {"registered_at": time.time()}
        self.data["sandboxes"] = sandboxes
        self._update()

    @property
    def run_id(self) -> str | None:
        return self.data.get("run_id")

    @property
    def template_id(self) -> str | None:
        return self.data.get("template_id")

    @property
    def run_started_at(self) -> float | None:
        value = self.data.get("run_started_at")
        return float(value) if isinstance(value, (int, float)) else None

    def registered_ids(self) -> set[str]:
        return set(self.data.get("sandboxes", {}))

    def registered_at(self, sandbox_id: str) -> float | None:
        entry = self.data.get("sandboxes", {}).get(sandbox_id, {})
        value = entry.get("registered_at")
        return float(value) if isinstance(value, (int, float)) else None


@dataclass
class _Target:
    record: SandboxRecord
    first_kill_at: float | None = field(default=None)


class NodeSupervisor:
    def __init__(
        self, client: SandboxClient, manifest: ManifestStore, *, alarm_file: Path
    ) -> None:
        self.client = client
        self.manifest = manifest
        self.alarm_file = alarm_file
        self._targets: dict[str, _Target] = {}
        self._last_now: float | None = None
        self._manifest_ok = True

    # ---- 告警（存在即阻止 PoC 客户端新建；确认后清除） ----
    def _raise_alarm(self, reason: str) -> None:
        self.alarm_file.parent.mkdir(parents=True, exist_ok=True)
        self.alarm_file.write_text(f"{time.time()} {reason}\n")
        LOG.error("ALARM %s: %s", self.alarm_file, reason)

    def _clear_alarm(self) -> None:
        with contextlib.suppress(OSError):
            self.alarm_file.unlink()

    # ---- 归属判定 ----
    def _classify(self, record: SandboxRecord) -> str | None:
        """返回归属级别说明；None=非本轮资源。marker（平台真相）优先。"""
        if record.run_marker is not None:
            return "marker" if record.run_marker == self.manifest.run_id else None
        if record.sandbox_id in self.manifest.registered_ids():
            return "registered"
        return None

    def _window_fallback(self, record: SandboxRecord) -> bool:
        """末级兜底：仅在 marker 完全缺失时；他轮 marker 是确定性"非本轮"。"""
        if record.run_marker is not None:
            return False
        if record.template_id != self.manifest.template_id:
            return False
        started, run_start = record.started_at, self.manifest.run_started_at
        if started is None or run_start is None:
            return False  # 无法限界 → 不兜底
        return started >= run_start - 5.0

    def _effective_start(self, record: SandboxRecord) -> float | None:
        if record.started_at is not None:
            return record.started_at
        return self.manifest.registered_at(record.sandbox_id)

    # ---- 主循环 ----
    def poll_once(self, now: float) -> list[str]:
        killed: list[str] = []
        if self._last_now is not None and now < self._last_now - 60:
            LOG.warning("clock jumped backwards: %.1fs", self._last_now - now)
        self._last_now = now

        try:
            self.manifest.reload()
            self._manifest_ok = True
        except ManifestError as exc:
            if self._manifest_ok:  # 只在状态翻转时告警一次
                self._raise_alarm(f"manifest corrupt; last-known kept: {exc}")
            self._manifest_ok = False

        try:
            records = self.client.list()
        except Exception as exc:
            self._raise_alarm(
                f"control plane unreachable; cannot guarantee termination: {type(exc).__name__}"
            )
            return killed

        reasons: list[str] = []
        seen: set[str] = set()
        for record in records:
            seen.add(record.sandbox_id)
            try:
                killed_id = self._process_record(record, now, reasons)
            except Exception:
                LOG.exception("target processing error (skipped)")
                killed_id = None
            if killed_id:
                killed.append(killed_id)

        # 告警状态按本轮条件重算：有原因 → 写；无原因且无待确认终止 → 清。
        if reasons:
            self._raise_alarm("; ".join(reasons))
        else:
            self._retire_targets(seen)
        return killed

    def _process_record(
        self, record: SandboxRecord, now: float, reasons: list[str]
    ) -> str | None:
        """单个目标：补齐 → 归属 → 截止判定 → 终止 → 宽限确认。

        告警原因追加到 reasons 供轮末统一写告警文件；返回被杀 ID 或 None。
        """
        record = self._hydrate_if_needed(record)
        if record is None:
            return None  # 单查 404：已消失

        target = self._targets.setdefault(record.sandbox_id, _Target(record=record))
        target.record = record

        def _alarm(reason: str) -> None:
            reasons.append(reason)
            LOG.error("ALARM reason: %s", reason)

        ours = self._resolve_ownership(record, _alarm)
        if ours is None or record.state in TERMINAL_STATES:
            return None

        start = self._effective_start(record)
        if start is None:
            # 创建时间不可得：不得以 0 判断，不删除，告警。
            _alarm(
                f"cannot determine start time; cannot guarantee termination: {record.sandbox_id}"
            )
            return None
        if now - start < DEADLINE_SECONDS:
            return None
        return self._enforce_deadline(target, now, _alarm)

    def _hydrate_if_needed(self, record: SandboxRecord) -> SandboxRecord | None:
        """v1 list 不含 startedAt/metadata：逐个单查补齐（PoC 规模小；
        后续可换 list_v2 服务端 metadata 过滤）。404 → None（已消失）。"""
        if record.run_marker is None or record.started_at is None:
            try:
                return self.client.hydrate(record)
            except NotFoundError:
                return None
        return record

    def _resolve_ownership(self, record: SandboxRecord, _alarm) -> str | None:
        """归属：marker 一级 / 登记二级 / 时间窗兜底（仅 marker 完全缺失时）。"""
        ours = self._classify(record)
        if ours is not None or record.run_marker is not None:
            return ours
        if self._window_fallback(record):
            LOG.warning("window-fallback used (marker+registry missing): %s", record.sandbox_id)
            return "window-fallback"
        if record.template_id == self.manifest.template_id:
            # 本轮模板但既无 marker/登记又无 startedAt：不可归类——告警，
            # 不扩大删除，也不静默忽略。
            _alarm(
                f"unclassifiable template match; cannot guarantee termination: {record.sandbox_id}"
            )
        return None

    def _enforce_deadline(self, target: _Target, now: float, _alarm) -> str | None:
        """已过截止：发终止并检查确认宽限。返回被杀 ID（未发出则 None）。"""
        if target.first_kill_at is None:
            target.first_kill_at = now
        issued = self._kill_with_retry(target.record.sandbox_id)
        if now - target.first_kill_at > CONFIRM_WINDOW_SECONDS:
            _alarm(
                f"confirm window exceeded; termination NOT confirmed: {target.record.sandbox_id}"
            )
        return target.record.sandbox_id if issued else None

    def _kill_with_retry(self, sandbox_id: str) -> bool:
        """发起终止。True=已发起或已确认消失；False=失败待下轮重试。"""
        try:
            self.client.kill(sandbox_id)
        except NotFoundError:
            LOG.info("sandbox gone (404): %s", sandbox_id)
        except Exception:
            # 单点失败不丢目标、不退出：下一轮重试。
            LOG.exception("kill failed (will retry): %s", sandbox_id)
            return False
        else:
            LOG.warning(
                "HARD DEADLINE: sandbox=%s (>= %ds) -> kill issued",
                sandbox_id,
                DEADLINE_SECONDS,
            )
        return True

    def _retire_targets(self, seen: set[str]) -> None:
        for sid in set(self._targets) - seen:
            del self._targets[sid]
        if not any(t.first_kill_at is not None for t in self._targets.values()):
            self._clear_alarm()  # 仅当无任何待确认终止时清除（告警原因为空已保证）

    def run_forever(self, *, clock=time.time) -> None:
        while True:
            self.poll_once(clock())
            time.sleep(POLL_INTERVAL_SECONDS)


def build_sdk_client(
    api_url: str = "http://127.0.0.1:3000",
    *,
    api_key: str | None = None,
    request_timeout: float = REQUEST_TIMEOUT_SECONDS,
) -> SandboxClient:
    """真实 v0.7.0 SDK 适配器（延迟导入；测试用假客户端不经此路径）。"""

    from cubesandbox import Sandbox
    from cubesandbox._config import Config
    from cubesandbox._exceptions import SandboxNotFoundError

    cfg = Config(api_url=api_url, api_key=api_key, request_timeout=request_timeout)

    class _SDKClient:
        def list(self) -> list[SandboxRecord]:
            items = Sandbox.list(config=cfg)  # list[dict]：sandboxID/templateID/state
            return [
                SandboxRecord(
                    sandbox_id=str(it.get("sandboxID", "")),
                    template_id=str(it.get("templateID", "")),
                    state=str(it.get("state", "")),
                )
                for it in items
            ]

        def hydrate(self, record: SandboxRecord) -> SandboxRecord:
            try:
                info = Sandbox({"sandboxID": record.sandbox_id}, cfg).get_info()
            except SandboxNotFoundError as exc:
                raise NotFoundError(str(exc)) from exc
            started = getattr(info, "started_at", None)
            metadata = getattr(info, "metadata", None) or {}
            return replace(
                record,
                started_at=started.timestamp() if started is not None else None,
                run_marker=metadata.get(RUN_MARKER_KEY),
            )

        def kill(self, sandbox_id: str) -> None:
            try:
                Sandbox({"sandboxID": sandbox_id}, cfg).kill()  # 不用 connect() 绕行
            except SandboxNotFoundError as exc:
                raise NotFoundError(str(exc)) from exc

    return _SDKClient()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--alarm-file", type=Path, required=True)
    parser.add_argument(
        "--register-run",
        nargs=3,
        metavar=("RUN_ID", "TEMPLATE_ALIAS", "TEMPLATE_ID"),
        help="登记新轮（重置旧登记；run_started_at=当前时间）",
    )
    parser.add_argument("--register-sandbox", metavar="SANDBOX_ID")
    parser.add_argument("--api-url", default="http://127.0.0.1:3000")
    parser.add_argument("--api-key", default=None)
    parser.add_argument("--once", action="store_true", help="执行一轮后退出")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    manifest = ManifestStore(args.manifest)
    if args.register_run:
        manifest.register_run(*args.register_run)
        LOG.info("run registered: %s alias=%s template=%s", *args.register_run)
        return
    if args.register_sandbox:
        manifest.register_sandbox(args.register_sandbox)
        LOG.info("sandbox registered: %s", args.register_sandbox)
        return

    supervisor = NodeSupervisor(
        build_sdk_client(args.api_url, api_key=args.api_key),
        manifest,
        alarm_file=args.alarm_file,
    )
    if args.once:
        supervisor.poll_once(time.time())  # 与常驻模式同用 epoch
    else:
        supervisor.run_forever()  # 默认 clock=time.time（epoch）


if __name__ == "__main__":
    main()
