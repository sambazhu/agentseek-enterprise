import sys
import time

import pytest
from agentseek_execution.models import Code, ContractError
from agentseek_execution.worker_process import MAX_OUTPUT, run_worker


def worker(code, request=b"", budget=2):
    return run_worker([sys.executable, "-c", code], request, budget=budget, environment={})


def test_worker_roundtrip_and_no_inherited_credentials(monkeypatch):
    monkeypatch.setenv("TEST_PRIVATE_CREDENTIAL", "must-not-inherit")
    result = worker(
        "import os,sys; assert 'TEST_PRIVATE_CREDENTIAL' not in os.environ; "
        "sys.stdout.buffer.write(sys.stdin.buffer.read())",
        b"private-request",
    )
    assert result == b"private-request"


@pytest.mark.parametrize(
    "code",
    [
        "import time; time.sleep(20)",
        "import sys,time; sys.stdout.write('partial'); sys.stdout.flush(); time.sleep(20)",
        "import os,time; os.close(1); time.sleep(20)",
        "import sys,time; [(sys.stdout.write('x'),sys.stdout.flush(),time.sleep(.05)) for _ in range(200)]",
    ],
)
def test_worker_total_deadline_and_unknown_outcome(code):
    started = time.monotonic()
    with pytest.raises(ContractError) as error:
        worker(code, budget=0.2)
    assert error.value.code == Code.UNKNOWN
    assert time.monotonic() - started < 1.5


def test_large_request_to_nonreading_worker_is_bounded():
    started = time.monotonic()
    with pytest.raises(ContractError):
        worker("import time; time.sleep(20)", b"x" * 65536, budget=0.2)
    assert time.monotonic() - started < 1.5


def test_nonzero_exit_and_stderr_are_not_exposed():
    with pytest.raises(ContractError) as error:
        worker("import sys; print('private-secret',file=sys.stderr); sys.exit(2)")
    assert str(error.value) == "outcome_unknown"


def test_oversized_output_rejected():
    with pytest.raises(ContractError):
        worker(f"import sys; sys.stdout.buffer.write(b'x'*{MAX_OUTPUT + 1})")


def test_worker_child_holding_pipe_does_not_escape_deadline():
    started = time.monotonic()
    with pytest.raises(ContractError):
        worker("import os,time; pid=os.fork(); time.sleep(20) if pid==0 else None", budget=0.2)
    assert time.monotonic() - started < 1.5


@pytest.mark.parametrize("budget", [0, -1, float("nan"), float("inf")])
def test_invalid_budget_rejected_before_launch(budget):
    with pytest.raises(ContractError) as error:
        worker("raise AssertionError('should not run')", budget=budget)
    assert error.value.code == Code.INVALID
