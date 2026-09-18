import pytest

from agentseek_execution.business_execution import BusinessRequest, run_business


class Backend:
    def __init__(self, failure=None):
        self.calls = []
        self.failure = failure
        self.reserved = False

    def call(self, name):
        self.calls.append(name)
        if self.failure == name:
            raise RuntimeError("synthetic private provider exception")

    def authorize(self, request):
        self.call("authorize")

    def reserve(self, request):
        self.call("reserve")
        if self.reserved:
            raise RuntimeError("already reserved")
        self.reserved = True
        return "attempt"

    def create(self, attempt):
        self.call("create")

    def prepare(self, attempt, ref):
        self.call("prepare")

    def execute(self, attempt, instruction):
        self.call("execute")
        return "result"

    def persist(self, attempt, result):
        self.call("persist")
        return "artifact"

    def destroy(self, attempt):
        self.call("destroy")
        return True

    def record(self, attempt, state, artifact):
        self.call("record")


REQUEST = BusinessRequest("request", "trusted-owner", "input-ref", "summarize csv")


def test_persistence_precedes_cleanup():
    backend = Backend()
    result = run_business(REQUEST, backend)
    assert backend.calls == ["authorize", "reserve", "create", "prepare", "execute", "persist", "destroy", "record"]
    assert result.state == "succeeded" and result.artifact_ref == "artifact"
    assert result.cleanup_confirmed


@pytest.mark.parametrize("failure", ["create", "prepare", "execute", "persist", "destroy"])
def test_failure_never_retries_and_always_attempts_cleanup(failure):
    backend = Backend(failure)
    result = run_business(REQUEST, backend)
    assert backend.calls.count("create") == 1
    assert backend.calls[-2:] == ["destroy", "record"]
    assert result.state == ("reconciling" if failure == "destroy" else "failed")


@pytest.mark.parametrize("failure", ["authorize", "reserve"])
def test_rejected_request_never_touches_platform(failure):
    backend = Backend(failure)
    with pytest.raises(RuntimeError):
        run_business(REQUEST, backend)
    assert "create" not in backend.calls and "destroy" not in backend.calls


def test_replay_cannot_create_again():
    backend = Backend()
    run_business(REQUEST, backend)
    with pytest.raises(RuntimeError):
        run_business(REQUEST, backend)
    assert backend.calls.count("create") == 1
