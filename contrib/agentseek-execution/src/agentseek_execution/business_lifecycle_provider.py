"""Bridge a pre-approved, same-host lifecycle to a fixed CSV execution session.

This is NOT a remote gateway broker. The trusted session factory must reconstruct
the guest from its sealed receipt and provide bounded command/termination I/O.
No session or endpoint is chosen by the model. Existing lifecycle admission,
quota, receipt and registration remain in force.
"""

from pathlib import Path
from dataclasses import asdict

from .m3_create_process import _path, _pinned
from .m3_create_worker import CreatePlan
from .m3_lifecycle import execute
from .m3_probe_process import _decode, config_bytes


class LifecycleBusinessProvider:
    def __init__(self, *, lifecycle_file: Path, lifecycle_sha256: str, session_for):
        self.path, self.digest, self.session_for = lifecycle_file, lifecycle_sha256, session_for
        self.config = _decode(_pinned(self.path, self.digest))
        if self.config.get("slot") != "A":
            raise ValueError("business pilot accepts only A")
        self.attempt = None
        self.session = None
        self.validated = False

    def validate_request(self, request, data):
        expected = self.session_for.validate_request(request, data)
        life = _decode(_pinned(self.path, self.digest))
        launcher = _decode(_pinned(_path(life["launcher"]), life["launcher_sha256"]))
        installed = _decode(_pinned(_path(launcher["create_file"]), launcher["create_sha256"]))
        precreate = _decode(_pinned(_path(installed["precreate_file"]), installed["precreate_sha256"]))
        if installed["schema"] != 2 or expected != asdict(CreatePlan(**precreate["plan"]).binding(installed["approval_sha256"])):
            raise ValueError("business/create approval mismatch")
        self.session_for.validate_installation(installed, precreate)
        self.validated = True

    def create(self, attempt):
        if self.attempt is not None or not self.validated:
            raise ValueError("provider already used")
        self.attempt = attempt
        result = execute(self.path, self.digest, "create")
        if result.get("saved") is not True:
            raise ValueError("creation not confirmed")
        saved = _decode(config_bytes(Path(self.config["directory"]) / "create-result.json"))
        if saved["config_sha256"] != self.digest or saved["result"]["registered_and_observed"] is not True:
            raise ValueError("creation receipt mismatch")
        # Trusted factory verifies the saved binding against the sealed vault.
        self.session = self.session_for(saved["result"]["binding"])

    def run(self, attempt, command, timeout):
        if attempt != self.attempt or self.session is None or timeout != 15:
            raise ValueError("no registered session")
        return self.session.run(command, timeout=timeout)

    def destroy(self, attempt):
        if attempt != self.attempt or self.session is None:
            # Lost creation/session reconstruction: no guessed target, retain
            # reconciliation state and leave the independent supervisor alive.
            return False
        try:
            self.session.terminate()
            result = execute(self.path, self.digest, "closeout")
            return result.get("known_target_absent") is True and result.get("next_create_authorized") is False
        finally:
            self.session.close()
