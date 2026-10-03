"""Single-writer PoC lifecycle controller. Only fixed synthetic intents/probes.

No real Work database, arbitrary shell, file upload or Volume API is exposed.
Global capacity is one, including uncertain creates. Journal/ledger boundaries
are reconciled explicitly; they are not one distributed transaction.
"""

from __future__ import annotations

from dataclasses import asdict
from uuid import UUID, uuid4

from .broker_access import CredentialRegistry
from .broker_clock import BrokerClock
from .cube_journal import CubeJournal
from .models import Code, ContractError, Execution, InputManifest, Scope, Task, require
from .secure_ledger import SecureLedger

ACTIVE = {"reserved", "creating", "running", "reconciling", "deleting"}


class Lifecycle:
    def __init__(
        self,
        credentials: CredentialRegistry,
        journal: CubeJournal,
        ledger: SecureLedger,
        clock: BrokerClock,
        worker,
        gate,
        *,
        deployment: str,
        template_id: str,
    ):
        self.credentials, self.journal, self.ledger, self.clock = credentials, journal, ledger, clock
        self.worker, self.gate, self.deployment = worker, gate, deployment
        self.template_id = template_id
        self.tick_error = False

    def _authorize(self, key: str, resource_id: str) -> dict:
        principal = self.credentials.authenticate(key)
        job = self.journal.require_job(resource_id)
        require(job["principal"] == principal.principal_id and Scope(**job["scope"]) in principal.scopes, Code.DENIED)
        return job

    def _execute_record(self, job: dict) -> Execution:
        task = Task(job["task_id"], Scope(**job["scope"]), job.get("work_id"), job.get("phase"))
        return Execution(job["resource_id"], task, InputManifest(), self.template_id, "m2-deny-egress")

    def _view(self, job: dict) -> dict:
        return {
            "resource_id": job["resource_id"],
            "task_id": job["task_id"],
            "kind": job["kind"],
            "state": job["state"],
            "execution_state": self.ledger.state(job["resource_id"]),
            "deadline": job["deadline"],
        }

    def create(self, key: str, request_id: str, kind: str) -> dict:
        principal = self.credentials.authenticate(key)
        require(kind in {"direct", "work"} and len(principal.scopes) == 1)
        require(UUID(request_id).hex == request_id)
        jobs = self.journal.all()
        prior = [j for j in jobs if j["principal"] == principal.principal_id and j["request_id"] == request_id]
        if prior:
            require(prior[0]["kind"] == kind, Code.CONFLICT)
            return self._view(self._authorize(key, prior[0]["resource_id"]))
        require(not self.tick_error and not any(j["state"] in ACTIVE for j in jobs), Code.CONFLICT)
        self.gate()  # trusted preflight wrapper: remote six checks + dedicated supervisor gate
        now = self.clock.now()
        resource_id = uuid4().hex
        job = {
            "resource_id": resource_id,
            "task_id": uuid4().hex,
            "principal": principal.principal_id,
            "scope": asdict(next(iter(principal.scopes))),
            "kind": kind,
            "request_id": request_id,
            "state": "reserved",
            "deadline": now + 120,
            "deployment": self.deployment,
        }
        if kind == "work":
            # Synthetic server-owned work reference; no WorkItem is written.
            job.update(work_id="m2-synthetic-" + uuid4().hex, phase="probe")
        self.journal.insert(resource_id, job)
        try:
            lease = self.ledger.begin(self._execute_record(job), self.deployment, now, 120)
            self.journal.patch(resource_id, state="creating", lease=asdict(lease))
            result = self.worker(resource_id, "create")
            require(result == {"status": "created"}, Code.UNKNOWN)
            receipt = self.journal.require_job(resource_id).get("receipt")
            require(bool(receipt and receipt.get("trafficAccessToken")), Code.UNKNOWN)
            self.ledger.transition(lease, "running", self.clock.now())
            job = self.journal.patch(resource_id, state="running")
        except Exception:
            # A reserved intent has not issued create; preserve that evidence.
            if self.journal.require_job(resource_id)["state"] != "reserved":
                self.journal.patch(resource_id, state="reconciling")
            raise ContractError(Code.UNKNOWN) from None
        return self._view(job)

    def operate(self, key: str, resource_id: str, operation: str) -> dict:
        job = self._authorize(key, resource_id)
        require(operation in {"inspect", "connect", "execute", "slow", "read", "write", "cancel", "delete"})
        now = self.clock.now()
        if operation in {"cancel", "delete"}:
            return self._delete(job)
        if operation == "inspect":
            return self._view(job)
        require(job["state"] == "running" and now < job["deadline"], Code.CONFLICT)
        lease = self.ledger.recover_lease(resource_id)
        if lease is None:
            raise ContractError(Code.CONFLICT)
        # Enforce stored fencing/expiry before touching the remote resource.
        with self.ledger.transaction():
            self.ledger._current(lease, now)
        remote_operation = job["kind"] if operation == "execute" else operation
        try:
            result = self.worker(resource_id, remote_operation)
            require(self.clock.now() < job["deadline"], Code.UNKNOWN)
            require(type(result) is dict and result.get("status") in {"running", "executed"}, Code.UNKNOWN)
        except Exception:
            self.journal.patch(resource_id, state="reconciling")
            raise ContractError(Code.UNKNOWN) from None
        return result

    def _delete(self, job: dict) -> dict:
        resource_id = job["resource_id"]
        if job["state"] == "stopped":
            return {"status": "stopped"}
        self.journal.patch(resource_id, state="deleting")
        try:
            result = self.worker(resource_id, "delete")
            require(result == {"status": "stopped"}, Code.UNKNOWN)
            lease = self.ledger.recover_lease(resource_id)
            if lease is not None and self.ledger.state(resource_id) not in {"cancelled", "succeeded"}:
                self.ledger.reconcile_stopped(lease, confirmed=True, cancelled=True)
            self.journal.patch(resource_id, state="stopped")
        except Exception:
            self.journal.patch(resource_id, state="reconciling")
            raise ContractError(Code.UNKNOWN) from None
        return {"status": "stopped"}

    def recover(self) -> None:
        """Restart never re-executes commands. Reconcile/stop all prior live jobs."""
        for job in self.journal.all():
            require(job["deployment"] == self.deployment, Code.DENIED)
            if job["state"] == "reserved":
                lease = self.ledger.recover_lease(job["resource_id"])
                if lease is not None:
                    self.ledger.reconcile_stopped(lease, confirmed=True)
                self.journal.patch(job["resource_id"], state="stopped")
            elif job["state"] in ACTIVE:
                self.journal.patch(job["resource_id"], state="reconciling")
        self.tick()

    def tick(self) -> None:
        self.tick_error = False
        try:
            now = self.clock.now()
            for job in self.journal.all():
                if job["state"] in ACTIVE and (job["deadline"] <= now or job["state"] in {"reconciling", "deleting"}):
                    try:
                        self._delete(job)
                    except ContractError:
                        self.tick_error = True
        except Exception:
            self.tick_error = True

    def __call__(self, request: dict) -> dict:
        require(type(request) is dict)
        key, method = request.get("key"), request.get("method")
        if not isinstance(key, str) or not isinstance(method, str):
            raise ContractError(Code.DENIED)
        principal = self.credentials.authenticate(key)
        if method == "list":
            require(set(request) == {"key", "method"})
            return {
                "resources": [
                    self._view(j)
                    for j in self.journal.all()
                    if j["principal"] == principal.principal_id and Scope(**j["scope"]) in principal.scopes
                ]
            }
        if method == "create":
            require(set(request) == {"key", "method", "request_id", "kind"})
            return self.create(key, request["request_id"], request["kind"])
        require(set(request) == {"key", "method", "resource_id"})
        return self.operate(key, request["resource_id"], method)
