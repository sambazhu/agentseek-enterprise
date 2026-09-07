"""PoC 节点侧硬截止监督（.172 侧）。

职责（对应 V0.1.3_M0_INSTALL_CHANGE_LIST.md §9.2，按 Codex 三次复核修订）：
- 单 VM 硬截止 120 秒：到时即触发终止（不是 150 秒）；
- 计时起点 = 平台记录的 created_at；轮询间隔 2 秒（最坏执行偏差 = 截止后
  2 秒内发出终止）；终止动作幂等（每轮重试直至确认消失）；
- 终止确认宽限 30 秒：仅用于确认销毁，绝不发起新命令；
- 精确资源身份：本轮 run_id + 已登记 sandbox ID 清单（manifest）为一级依据；
  模板 alias 仅作 catch-all（客户端断连未登记时兜底），不是唯一依据；
- 杀灭对象 = manifest 内 ID ∪ alias 匹配者，且仅限超过截止时刻的实例——
  不触碰任何其他资源；
- 独立性声明：独立于 .171 客户端与被 systemd 限额的服务；与 .172 整机
  同故障域（不作"不同故障域"表述）；
- 查询/终止接口：cubesandbox SDK（Sandbox.list / sandbox.kill），控制面走
  127.0.0.1:3000 loopback（携带 X-API-Key，与远程入口同一密值）。

客户端实现注入（SandboxClient 协议），定向测试（test_node_supervisor.py）
使用假客户端与假时钟，不依赖真实 SDK。
"""

from __future__ import annotations

import argparse
import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

LOG = logging.getLogger("cube_poc_supervisor")

DEADLINE_SECONDS = 120  # 单 VM 硬截止（冻结值）
POLL_INTERVAL_SECONDS = 2  # 轮询间隔（终止执行最坏偏差）
CONFIRM_WINDOW_SECONDS = 30  # 终止确认宽限（仅确认，不执行新命令）


@dataclass(frozen=True)
class SandboxRecord:
    sandbox_id: str
    template_alias: str
    created_at: float
    status: str


class SandboxClient(Protocol):
    def list(self) -> list[SandboxRecord]: ...

    def kill(self, sandbox_id: str) -> None: ...


class ManifestStore:
    """run_id/alias/已登记 sandbox ID 的登记处（JSON 文件）。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        if path.exists():
            self.data = json.loads(path.read_text())
        else:
            self.data = {"run_id": None, "template_alias": None, "sandbox_ids": []}

    def register_run(self, run_id: str, template_alias: str) -> None:
        self.data["run_id"] = run_id
        self.data["template_alias"] = template_alias
        self._flush()

    def register_sandbox(self, sandbox_id: str) -> None:
        if sandbox_id not in self.data["sandbox_ids"]:
            self.data["sandbox_ids"].append(sandbox_id)
            self._flush()

    def _flush(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, indent=2))

    @property
    def ids(self) -> set[str]:
        return set(self.data["sandbox_ids"])

    @property
    def alias(self) -> str | None:
        return self.data["template_alias"]


class NodeSupervisor:
    def __init__(self, client: SandboxClient, manifest: ManifestStore) -> None:
        self.client = client
        self.manifest = manifest

    def _is_ours(self, record: SandboxRecord) -> bool:
        # 一级依据：精确 ID 登记；catch-all：本轮模板 alias（客户端断连兜底）。
        if record.sandbox_id in self.manifest.ids:
            return True
        alias = self.manifest.alias
        return bool(alias) and record.template_alias == alias

    def poll_once(self, now: float) -> list[str]:
        """执行一轮检查。返回本轮发出终止的 sandbox_id 列表（幂等重试无害）。"""
        killed: list[str] = []
        for record in self.client.list():
            if not self._is_ours(record):
                continue
            age = now - record.created_at
            if age < DEADLINE_SECONDS:
                continue  # 未到硬截止：不动作（宽限不提前、不延长执行）
            if record.status in ("terminated", "removed"):
                continue
            self.client.kill(record.sandbox_id)
            killed.append(record.sandbox_id)
            LOG.warning(
                "HARD DEADLINE: sandbox=%s age=%.1fs (>=%ds) -> kill issued",
                record.sandbox_id,
                age,
                DEADLINE_SECONDS,
            )
        return killed

    def run_forever(self, *, clock=time.monotonic) -> None:
        while True:
            self.poll_once(clock())
            time.sleep(POLL_INTERVAL_SECONDS)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--register-run", nargs=2, metavar=("RUN_ID", "ALIAS"))
    parser.add_argument("--register-sandbox", metavar="SANDBOX_ID")
    parser.add_argument("--once", action="store_true", help="执行一轮后退出")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    manifest = ManifestStore(args.manifest)
    if args.register_run:
        manifest.register_run(*args.register_run)
        LOG.info("run registered: %s alias=%s", *args.register_run)
        return
    if args.register_sandbox:
        manifest.register_sandbox(args.register_sandbox)
        LOG.info("sandbox registered: %s", args.register_sandbox)
        return

    def _build_client() -> SandboxClient:
        # 运行态使用 cubesandbox SDK 指向 loopback CubeAPI（安装轮核验字段）。
        from cubesandbox import Sandbox  # 延迟导入，测试不依赖
        from cubesandbox._config import Config

        cfg = Config(api_url="http://127.0.0.1:3000")

        class _SDKClient:
            def list(self) -> list[SandboxRecord]:
                items = Sandbox.list(config=cfg)
                return [
                    SandboxRecord(
                        sandbox_id=s.sandbox_id,
                        template_alias=getattr(s, "template_alias", ""),
                        created_at=float(getattr(s, "created_at", 0)),
                        status=str(getattr(s, "status", "")),
                    )
                    for s in items
                ]

            def kill(self, sandbox_id: str) -> None:
                Sandbox(sandbox_id=sandbox_id, config=cfg).kill()

        return _SDKClient()

    supervisor = NodeSupervisor(_build_client(), manifest)
    if args.once:
        supervisor.poll_once(time.time())
    else:
        supervisor.run_forever()


if __name__ == "__main__":
    main()
