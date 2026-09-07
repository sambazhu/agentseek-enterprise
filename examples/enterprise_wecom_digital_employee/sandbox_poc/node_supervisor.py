"""PoC 节点侧硬截止监督（.172 侧）——v3，按 Codex 五次复核（P1×4/P2）修复。

合同（V0.1.3_M0_INSTALL_CHANGE_LIST.md §9.2）：
- 单 VM 硬截止 120 秒；宽限 30 秒仅用于终止确认；
- **单调时钟 deadline**：首次观测到平台 startedAt 时换算为单调钟截止点；
  此后墙钟（epoch）回拨不会延长执行、前跳不会提前终止；终止判定与
  终止执行前均以单调钟复核；
- **终止授权（仅两级，无模板兜底）**：本轮 metadata run 标记（create 原生
  参数，响应丢失亦在服务端留痕）或 manifest 精确 ID 登记；marker 缺失且
  未登记 → 只告警阻断新建，**绝不纳入删除集合**（同模板也不行）；
  他轮 marker 始终排除；
- **告警状态机**：manifest 无效 / 控制面不可达 / 同模板不可归类 / 起始时间
  未知 / kill 失败 / 确认宽限超时均为显式状态，按轮重算——任一未解除即
  保持告警文件（存在=阻止 .171 新建）；全部解除且无待确认终止才清除；
- **创建门禁**：--gate-check 供 .171 PoC 客户端在建前调用（建议经 SSH），
  告警存在 / manifest 无效 / 检查不可达 → 拒绝新建（失败关闭）；
- **请求有界超时在真实 HTTP 层生效**：v0.7.0 SDK 的控制面调用不传
  timeout（源码核对），故监督使用自带超时的极薄 HTTP 客户端（同一线上
  协议：GET /sandboxes、GET /sandboxes/{id}、DELETE /sandboxes/{id}，
  字段以 SDK v0.7.0 源码为准）；SDK 仍用于 .171 PoC 客户端；
- **最坏终止延迟**（如实声明）：单调 deadline +（本轮在途目标处理时间
  （每目标 ≤ 1×info + 1×kill 请求上限）+ 轮询间隔）；终止以执行前单调钟
  复核，不使用轮首轮快照；
- manifest：独立锁文件 flock（读共享/写独占）+ 同目录临时文件 fsync +
  os.replace 原子替换 + 目录 fsync；写入校验旧轮拒绝（run_id 不匹配即
  拒绝登记）；reload 全 schema 校验（含时间值为有限数值）；
- 独立性：独立于 .171 客户端与被 systemd 限额的服务；与 .172 整机同故障域。
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import http.client
import json
import logging
import math
import os
import socket
import threading
import time
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Protocol

LOG = logging.getLogger("cube_poc_supervisor")

DEADLINE_SECONDS = 120  # 单 VM 硬截止（冻结值）
POLL_INTERVAL_SECONDS = 2  # 轮询间隔
CONFIRM_WINDOW_SECONDS = 30  # 终止确认宽限（仅确认，不执行新命令）
REQUEST_TIMEOUT_SECONDS = 10  # 单次控制面请求硬超时（HTTP 层生效）
RUN_MARKER_KEY = "agentseek_run_id"  # create metadata 随请求携带的本轮标识
HEARTBEAT_MAX_AGE_SECONDS = (
    3 * POLL_INTERVAL_SECONDS + 2 * REQUEST_TIMEOUT_SECONDS + 5
)  # 心跳过期阈值（3 轮 + 2 次请求上限 + 余量）
TERMINAL_STATES = ("terminated", "removed")


class NotFoundError(Exception):
    """目标已不存在（HTTP 404）——视为已确认消失。"""


class ManifestError(Exception):
    """manifest 损坏/非法——保持告警，绝不扩大删除范围。"""


@dataclass
class SandboxRecord:
    sandbox_id: str
    template_id: str
    state: str
    started_at: float | None = None  # epoch；None=未知，不得以 0 参与判断
    run_marker: str | None = None  # metadata 中的本轮标识


class SandboxClient(Protocol):
    def list(self) -> list[SandboxRecord]: ...

    def hydrate(self, record: SandboxRecord) -> SandboxRecord: ...

    def kill(self, sandbox_id: str) -> None: ...


# --------------------------------------------------------------------------
# 有界超时 HTTP 客户端（替代 SDK 控制面调用：SDK v0.7.0 不传 timeout）
# --------------------------------------------------------------------------
MAX_RESPONSE_BYTES = 4 * 1024 * 1024  # 响应体上限（列表/信息均为小 JSON）


def _deadline_exceeded(path: str) -> TimeoutError:
    return TimeoutError(f"request total deadline exceeded: {path}")


class HttpCubeClient:
    """极薄 CubeAPI 客户端：单调钟总截止覆盖连接/响应头/响应体。

    urllib 的 timeout 只约束单个阻塞操作，慢滴流可绕过；本实现用
    http.client + 每 chunk 重设剩余预算的 socket timeout，保证单请求
    总耗时 <= timeout + eps，并对响应体设大小上限。

    线协议与 v0.7.0 SDK 一致（源码核对）：GET /sandboxes -> JSON 列表；
    GET /sandboxes/{id} -> {"startedAt","metadata"}；DELETE -> 204/200；
    404 -> NotFoundError。非法响应显式抛错，不静默解释为空集合。
    """

    def __init__(
        self,
        api_url: str = "http://127.0.0.1:3000",
        *,
        api_key: str | None = None,
        timeout: float = REQUEST_TIMEOUT_SECONDS,
        max_response_bytes: int = MAX_RESPONSE_BYTES,
    ) -> None:
        self.api_url = api_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self.max_response_bytes = max_response_bytes
        parts = urllib.parse.urlsplit(self.api_url)
        self._scheme = parts.scheme or "http"
        self._host = parts.hostname or "127.0.0.1"
        self._port = parts.port or (443 if self._scheme == "https" else 80)
        self._base_path = parts.path.rstrip("/")

    def _request(self, method: str, path: str) -> object:
        # 受控端点：URL 仅由本组件拼接（loopback/内网 api_url + 白名单路径）。
        # 总截止由独立看门狗保证：到点对连接 shutdown(SHUT_RDWR)，可中断
        # getresponse 内部的状态行/响应头/chunk 控制行读取（这些内部 readline
        # 在持续滴流下永远不会触发单次 socket 超时）；body 循环另以剩余预算
        # 快速失败。
        deadline = time.monotonic() + self.timeout
        conn_cls = (
            http.client.HTTPSConnection
            if self._scheme == "https"
            else http.client.HTTPConnection
        )
        conn = conn_cls(self._host, self._port, timeout=self.timeout)
        fired = threading.Event()

        def _kill() -> None:
            fired.set()
            with contextlib.suppress(Exception):
                if conn.sock is not None:
                    conn.sock.shutdown(socket.SHUT_RDWR)

        watchdog = threading.Timer(self.timeout, _kill)
        watchdog.daemon = True
        try:
            watchdog.start()
            headers = {"Accept": "application/json"}
            if self.api_key:
                headers["X-API-Key"] = self.api_key
            conn.request(method, f"{self._base_path}{path}", headers=headers)
            self._apply_remaining_timeout(conn, deadline, path)
            resp = conn.getresponse()
            if resp.status == 404:
                raise NotFoundError(path)
            if resp.status >= 400:
                err = RuntimeError(f"http {resp.status}: {path}")
                raise err
            body = self._read_bounded(conn, resp, deadline, path)
        except (TimeoutError, OSError, http.client.HTTPException) as exc:
            # 看门狗中断后 http.client 可能将损坏流翻译为 HTTPException 家族
            # （BadStatusLine/IncompleteRead/LineTooLong/RemoteDisconnected），
            # 过截止或看门狗已触发时一律归一为总截止超时。
            if fired.is_set() or time.monotonic() > deadline:
                raise _deadline_exceeded(path) from exc
            raise
        finally:
            watchdog.cancel()  # 完成即取消，不遗留后台任务
            conn.close()  # 超时/成功均关闭连接
        if fired.is_set() and time.monotonic() > deadline:
            raise _deadline_exceeded(path)
        if not body:
            return None
        return json.loads(body)

    def _apply_remaining_timeout(self, conn, deadline: float, path: str) -> None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise _deadline_exceeded(path)
        if conn.sock is not None:
            conn.sock.settimeout(remaining)

    def _read_bounded(self, conn, resp, deadline: float, path: str) -> bytes:
        chunks: list[bytes] = []
        total = 0
        while True:
            self._apply_remaining_timeout(conn, deadline, path)
            # read1：单次底层读即返回（read(amt) 会等待凑满 amt 字节，
            # 慢滴流下永不返回，绕过 socket 超时）。
            chunk = resp.read1(65536)
            if not chunk:
                break
            total += len(chunk)
            if total > self.max_response_bytes:
                raise RuntimeError(f"response body exceeds limit: {path}")
            chunks.append(chunk)
        return b"".join(chunks)

    def list(self) -> list[SandboxRecord]:
        items = self._request("GET", "/sandboxes")
        if not isinstance(items, list):
            raise TypeError("malformed /sandboxes response: not a list")
        records = []
        for it in items:
            if not isinstance(it, dict) or not it.get("sandboxID"):
                raise TypeError("malformed /sandboxes entry: missing sandboxID")
            records.append(
                SandboxRecord(
                    sandbox_id=str(it.get("sandboxID", "")),
                    template_id=str(it.get("templateID", "")),
                    state=str(it.get("state", "")),
                )
            )
        return records

    def hydrate(self, record: SandboxRecord) -> SandboxRecord:
        info = self._request("GET", f"/sandboxes/{record.sandbox_id}")
        if not isinstance(info, dict):
            raise TypeError(f"malformed sandbox info: {record.sandbox_id}")
        started_raw = info.get("startedAt")
        metadata = info.get("metadata") or {}
        return replace(
            record,
            started_at=_parse_epoch(started_raw),
            run_marker=metadata.get(RUN_MARKER_KEY),
        )

    def kill(self, sandbox_id: str) -> None:
        self._request("DELETE", f"/sandboxes/{sandbox_id}")


def _parse_epoch(value: object) -> float | None:
    """ISO8601（含 Z 后缀）→ epoch；非法/缺失 → None（不得默认 0）。"""
    if isinstance(value, (int, float)) and math.isfinite(value):
        return float(value)
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.timestamp()
    except ValueError:
        return None


# --------------------------------------------------------------------------
# manifest：独立锁文件 + 原子替换 + schema 校验 + 旧轮写入拒绝
# --------------------------------------------------------------------------
def _invalid(message: str) -> ManifestError:
    return ManifestError(message)


def _validate_manifest(data: object) -> dict:
    if not isinstance(data, dict):
        raise _invalid("root not object")
    for key in ("run_id", "template_alias", "template_id"):
        if data.get(key) is not None and not isinstance(data.get(key), str):
            raise _invalid(f"{key} not str|null")
    started = data.get("run_started_at")
    if started is not None and (
        not isinstance(started, (int, float)) or not math.isfinite(started)
    ):
        raise _invalid("run_started_at not finite number")
    sandboxes = data.get("sandboxes", {})
    if not isinstance(sandboxes, dict):
        raise _invalid("sandboxes not object")
    for sid, entry in sandboxes.items():
        if not isinstance(entry, dict):
            raise _invalid(f"entry {sid} not object")
        registered = entry.get("registered_at")
        if not isinstance(registered, (int, float)) or not math.isfinite(registered):
            raise _invalid(f"entry {sid} registered_at not finite")
    return data


class ManifestStore:
    """每轮一个 JSON 文件；独立 .lock 文件 flock；临时文件原子替换。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock_path = path.with_suffix(path.suffix + ".lock")
        self.data: dict = {}
        self.reload()

    def _read_shared(self) -> dict:
        with open(self.lock_path, "a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_SH)
            try:
                try:
                    with open(self.path) as fh:
                        raw = fh.read()
                except FileNotFoundError:
                    return {}
                return _validate_manifest(json.loads(raw) if raw else {})
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def reload(self) -> None:
        try:
            self.data = self._read_shared()
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            err = ManifestError(str(exc))
            raise err from exc

    def _atomic_write_exclusive(self, *, fresh: bool) -> None:
        """独占锁内：重读校验基线（含旧轮拒绝）→ 临时文件 fsync → 原子替换。"""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.lock_path, "a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                try:
                    with open(self.path) as fh:
                        raw = fh.read()
                    base = _validate_manifest(json.loads(raw)) if raw else {}
                except FileNotFoundError:
                    base = {}
                if not fresh:
                    if base.get("run_id") != self.data.get("run_id"):
                        raise _invalid(
                            f"stale run: file={base.get('run_id')!r} writer={self.data.get('run_id')!r}"
                        )
                    merged = dict(self.data)
                    merged["sandboxes"] = {
                        **base.get("sandboxes", {}),
                        **self.data.get("sandboxes", {}),
                    }
                    self.data = merged
                tmp = self.path.with_name(f"{self.path.name}.tmp-{os.getpid()}")
                with open(tmp, "w") as out:
                    json.dump(self.data, out, indent=2)
                    out.flush()
                    os.fsync(out.fileno())
                os.replace(tmp, self.path)
                with contextlib.suppress(OSError):
                    dirfd = os.open(self.path.parent, os.O_RDONLY)
                    try:
                        os.fsync(dirfd)
                    finally:
                        os.close(dirfd)
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

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
            "sandboxes": {},  # 新轮重置旧登记（fresh 覆盖，不做合并）
        }
        self._atomic_write_exclusive(fresh=True)

    def register_sandbox(self, sandbox_id: str) -> None:
        if not self.data.get("run_id"):
            raise _invalid("register_sandbox before register_run")
        sandboxes = dict(self.data.get("sandboxes", {}))
        if sandbox_id not in sandboxes:
            sandboxes[sandbox_id] = {"registered_at": time.time()}
        self.data["sandboxes"] = sandboxes
        self._atomic_write_exclusive(fresh=False)  # 旧轮/异轮写入在此被拒

    @property
    def run_id(self) -> str | None:
        return self.data.get("run_id")

    @property
    def template_id(self) -> str | None:
        return self.data.get("template_id")

    def registered_ids(self) -> set[str]:
        return set(self.data.get("sandboxes", {}))

    def registered_at(self, sandbox_id: str) -> float | None:
        entry = self.data.get("sandboxes", {}).get(sandbox_id, {})
        value = entry.get("registered_at")
        return float(value) if isinstance(value, (int, float)) else None


# --------------------------------------------------------------------------
# 监督主体
# --------------------------------------------------------------------------
@dataclass
class _Target:
    record: SandboxRecord
    deadline_mono: float | None = None  # 单调钟截止点（一经确定不再变）
    first_kill_mono: float | None = None


class NodeSupervisor:
    def __init__(
        self,
        client: SandboxClient,
        manifest: ManifestStore,
        *,
        alarm_file: Path,
        heartbeat_file: Path | None = None,
        epoch_clock: Callable[[], float] = time.time,
        mono_clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.client = client
        self.manifest = manifest
        self.alarm_file = alarm_file
        self.heartbeat_file = heartbeat_file or manifest.path.with_suffix(
            manifest.path.suffix + ".heartbeat"
        )
        self.epoch_clock = epoch_clock
        self.mono_clock = mono_clock
        self._targets: dict[str, _Target] = {}

    def _write_alarm(self, text: str) -> None:
        self.alarm_file.parent.mkdir(parents=True, exist_ok=True)
        self.alarm_file.write_text(f"{time.time()} {text}\n")
        LOG.error("ALARM %s: %s", self.alarm_file, text)

    def _clear_alarm(self) -> None:
        with contextlib.suppress(OSError):
            self.alarm_file.unlink()
            LOG.info("alarm cleared (all states resolved)")

    # ---- 归属（仅两级；无模板兜底） ----
    def _classify(self, record: SandboxRecord) -> str | None:
        if record.run_marker is not None:
            return "marker" if record.run_marker == self.manifest.run_id else None
        if record.sandbox_id in self.manifest.registered_ids():
            return "registered"
        return None

    def _deadline_mono_for(self, target: _Target, start_epoch: float) -> float:
        if target.deadline_mono is None:
            epoch_now = self.epoch_clock()
            mono_now = self.mono_clock()
            # 首次观测换算：此后墙钟回拨/前跳不影响该截止点。
            target.deadline_mono = mono_now + (start_epoch + DEADLINE_SECONDS - epoch_now)
        return target.deadline_mono

    def poll_once(self) -> list[str]:
        killed: list[str] = []
        reasons: list[str] = []

        try:
            self.manifest.reload()
        except ManifestError as exc:
            reasons.append(f"manifest invalid: {exc}")

        records: list[SandboxRecord] = []
        list_ok = False
        try:
            records = self.client.list()
            list_ok = True  # 仅"成功且通过 schema 校验"的列表可作为消失依据
        except Exception as exc:
            reasons.append(f"control plane unreachable; cannot guarantee termination: {type(exc).__name__}")

        seen: set[str] = set()
        for record in records:
            seen.add(record.sandbox_id)
            try:
                killed_id = self._process_record(record, reasons)
            except Exception:
                LOG.exception("target processing error")
                reasons.append(f"processing error: {record.sandbox_id}")
                killed_id = None
            if killed_id:
                killed.append(killed_id)

        # 先清理已消失目标，再计算待确认状态（否则消失后多挂一轮告警）。
        # list 失败/畸形时保留全部已知目标（deadline 与确认计时不重置）。
        if list_ok:
            for sid in set(self._targets) - seen:
                del self._targets[sid]
        pending = any(t.first_kill_mono is not None for t in self._targets.values())
        if reasons or pending:
            parts = list(reasons) + (["termination pending confirmation"] if pending else [])
            self._write_alarm("; ".join(parts))
        else:
            self._clear_alarm()
        self._write_heartbeat()
        return killed

    def _write_heartbeat(self) -> None:
        """监督存活心跳：与 run_id 关联；门禁据此验活（过期即拒绝新建）。"""
        payload = {
            "run_id": self.manifest.run_id,
            "ts": self.epoch_clock(),
            "pid": os.getpid(),
        }
        tmp = self.heartbeat_file.with_name(f"{self.heartbeat_file.name}.tmp")
        with open(tmp, "w") as out:
            json.dump(payload, out)
            out.flush()
            os.fsync(out.fileno())
        os.replace(tmp, self.heartbeat_file)

    def _process_record(self, record: SandboxRecord, reasons: list[str]) -> str | None:
        record = self._hydrate_if_needed(record, reasons)
        if record is None:
            return None
        target = self._targets.setdefault(record.sandbox_id, _Target(record=record))
        target.record = record

        ours = self._classify(record)
        if ours is None:
            if record.run_marker is None and record.template_id == self.manifest.template_id:
                # 同模板、无标记、未登记：不删除，显式告警阻断新建。
                reasons.append(
                    f"unclassified same-template instance (no marker, unregistered): {record.sandbox_id}"
                )
            return None
        if record.state in TERMINAL_STATES:
            return None

        start = record.started_at
        if start is None:
            start = self.manifest.registered_at(record.sandbox_id)
        if start is None:
            reasons.append(
                f"cannot determine start time; cannot guarantee termination: {record.sandbox_id}"
            )
            return None

        deadline = self._deadline_mono_for(target, start)
        if self.mono_clock() < deadline:
            return None
        return self._enforce_deadline(target, reasons)

    def _hydrate_if_needed(self, record: SandboxRecord, reasons: list[str]) -> SandboxRecord | None:
        if record.run_marker is not None and record.started_at is not None:
            return record
        try:
            return self.client.hydrate(record)
        except NotFoundError:
            return None  # 已消失
        except Exception:
            LOG.exception("hydrate failed")
            reasons.append(f"query error: {record.sandbox_id}")
            return None

    def _enforce_deadline(self, target: _Target, reasons: list[str]) -> str | None:
        sandbox_id = target.record.sandbox_id
        mono_now = self.mono_clock()  # 执行前以单调钟复核（不用轮初快照）
        if target.first_kill_mono is None:
            target.first_kill_mono = mono_now
        try:
            self.client.kill(sandbox_id)
            LOG.warning("HARD DEADLINE: sandbox=%s -> kill issued", sandbox_id)
            issued = True
        except NotFoundError:
            LOG.info("sandbox gone (404): %s", sandbox_id)
            issued = True
        except Exception:
            LOG.exception("kill failed (will retry)")
            reasons.append(f"kill failed: {sandbox_id}")
            issued = False
        if mono_now - target.first_kill_mono > CONFIRM_WINDOW_SECONDS:
            reasons.append(f"confirm window exceeded; termination NOT confirmed: {sandbox_id}")
        return sandbox_id if issued else None

    def run_forever(self) -> None:
        while True:
            self.poll_once()
            time.sleep(POLL_INTERVAL_SECONDS)


# --------------------------------------------------------------------------
# 创建门禁（.171 PoC 客户端建前调用；失败关闭）
# --------------------------------------------------------------------------
def check_gate(
    manifest_path: Path,
    alarm_file: Path,
    *,
    heartbeat_path: Path | None = None,
    now: float | None = None,
    max_age: float = HEARTBEAT_MAX_AGE_SECONDS,
) -> tuple[bool, str]:
    """创建门禁（失败关闭）。

    允许新建的必要条件：无告警 + manifest 有效且有 run_id + **监督存活**
    （心跳存在、可读、run_id 匹配、足够新鲜）。监督未启动/已退出/心跳
    过期/旧轮心跳/状态不可读 → 一律拒绝。
    """
    try:
        if alarm_file.exists():
            return False, alarm_file.read_text().strip()
    except OSError as exc:
        return False, f"alarm check failed: {exc}"
    if not manifest_path.exists():
        return False, "manifest missing (no active run)"
    try:
        store = ManifestStore(manifest_path)
        store.reload()
    except ManifestError as exc:
        return False, f"manifest invalid: {exc}"
    except Exception as exc:  # 门禁失败关闭
        return False, f"gate check failed: {type(exc).__name__}"
    if not store.run_id:
        return False, "manifest has no active run_id"

    beat_path = heartbeat_path or manifest_path.with_suffix(
        manifest_path.suffix + ".heartbeat"
    )
    return _heartbeat_gate(beat_path, store.run_id, now, max_age)


def _heartbeat_gate(
    beat_path: Path, expected_run: str, now: float | None, max_age: float
) -> tuple[bool, str]:
    try:
        beat = json.loads(beat_path.read_text())
    except FileNotFoundError:
        return False, "supervisor not alive (no heartbeat)"
    except (OSError, ValueError, json.JSONDecodeError):
        return False, "heartbeat unreadable"
    if beat.get("run_id") != expected_run:
        return False, f"heartbeat run mismatch: {beat.get('run_id')!r}"
    ts = beat.get("ts")
    if not isinstance(ts, (int, float)) or not math.isfinite(ts):
        return False, "heartbeat ts invalid"
    age = (now if now is not None else time.time()) - ts
    if age > max_age or age < -max_age:
        return False, f"heartbeat stale (age {age:.0f}s)"
    return True, ""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--alarm-file", type=Path, required=True)
    parser.add_argument(
        "--register-run", nargs=3, metavar=("RUN_ID", "TEMPLATE_ALIAS", "TEMPLATE_ID")
    )
    parser.add_argument("--register-sandbox", metavar="SANDBOX_ID")
    parser.add_argument("--api-url", default="http://127.0.0.1:3000")
    parser.add_argument("--api-key", default=None)
    parser.add_argument("--once", action="store_true", help="执行一轮后退出")
    parser.add_argument(
        "--gate-check", action="store_true",
        help="创建门禁：0=允许，3=阻断（供 .171 建前经 SSH 调用）",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if args.gate_check:
        allowed, reason = check_gate(args.manifest, args.alarm_file)
        if allowed:
            print("GATE: ALLOWED")
            return
        print(f"GATE: BLOCKED {reason}")  # 原因为告警内容，不含凭据
        raise SystemExit(3)

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
        HttpCubeClient(args.api_url, api_key=args.api_key),
        manifest,
        alarm_file=args.alarm_file,
    )
    if args.once:
        supervisor.poll_once()
    else:
        supervisor.run_forever()


if __name__ == "__main__":
    main()
