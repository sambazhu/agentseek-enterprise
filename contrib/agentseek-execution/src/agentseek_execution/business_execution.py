"""Business lifecycle seam, not a configured production sandbox provider.

Trusted adapters resolve identity, grant and input references before invocation.
The model must never supply providers, credentials, ownership or storage paths.
"""

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class BusinessRequest:
    request_id: str
    owner_id: str
    input_ref: str
    instruction: str


class BusinessBackend(Protocol):
    def authorize(self, request: BusinessRequest) -> None: ...
    def reserve(self, request: BusinessRequest) -> str:
        """Durable, exclusive attempt; replay must not start a second create."""
        ...
    def create(self, attempt: str) -> None:
        """Record remote identity durably; uncertainty remains reconcilable."""
        ...
    def prepare(self, attempt: str, input_ref: str) -> None: ...
    def execute(self, attempt: str, instruction: str) -> str:
        """Return an opaque result reference, never a host path or credential."""
        ...
    def persist(self, attempt: str, result_ref: str) -> str:
        """Commit bytes and verify durable read-back; return artifact reference."""
        ...
    def destroy(self, attempt: str) -> bool:
        """Bounded cleanup of the recorded target; true only for confirmed absence."""
        ...
    def record(self, attempt: str, state: str, artifact_ref: str | None) -> None: ...


@dataclass(frozen=True)
class BusinessOutcome:
    attempt: str
    state: str
    artifact_ref: str | None
    cleanup_confirmed: bool


def run_business(request: BusinessRequest, backend: BusinessBackend) -> BusinessOutcome:
    """Persist before deletion; never retry an uncertain create or execution.

Independent deadline supervision remains mandatory in the concrete backend.
This seam does not turn synthetic adapters into real platform evidence.
"""
    backend.authorize(request)
    attempt = backend.reserve(request)
    artifact = None
    state = "failed"
    cleanup = False
    try:
        backend.create(attempt)
        backend.prepare(attempt, request.input_ref)
        result = backend.execute(attempt, request.instruction)
        artifact = backend.persist(attempt, result)
        if not isinstance(artifact, str) or not artifact:
            raise ValueError("missing committed artifact reference")
        state = "succeeded"
    except Exception:
        # No raw provider exception is exposed to the agent.
        state = "failed"
    finally:
        try:
            cleanup = backend.destroy(attempt) is True
        except Exception:
            cleanup = False
        if not cleanup:
            state = "reconciling"
        backend.record(attempt, state, artifact)
    return BusinessOutcome(attempt, state, artifact, cleanup)
