from types import SimpleNamespace

import pytest

from agentseek_execution import business_lifecycle_provider as module


def provider(monkeypatch):
    monkeypatch.setattr(module, "_pinned", lambda *a: b'{"slot":"A","directory":"/unused"}')
    return module.LifecycleBusinessProvider(lifecycle_file=module.Path("/unused.json"),
                                           lifecycle_sha256="a"*64, session_for=lambda b: None)


def test_unvalidated_business_request_never_creates(monkeypatch):
    p = provider(monkeypatch)
    monkeypatch.setattr(module, "execute", lambda *a: pytest.fail("must not create"))
    with pytest.raises(ValueError):
        p.create("attempt")
    assert p.destroy("attempt") is False


def test_cleanup_requires_termination_and_closeout(monkeypatch):
    p = provider(monkeypatch)
    events = []
    p.attempt = "attempt"
    p.session = SimpleNamespace(terminate=lambda: events.append("terminate"), close=lambda: events.append("close"))
    def execute(*args):
        assert args[-1] == "closeout"
        events.append("closeout")
        return dict(known_target_absent=True, next_create_authorized=False)
    monkeypatch.setattr(module, "execute", execute)
    assert p.destroy("attempt") is True
    assert events == ["terminate", "closeout", "close"]


def test_lost_delete_response_is_not_cleanup_success(monkeypatch):
    p = provider(monkeypatch)
    events = []
    def fail():
        raise TimeoutError()
    p.attempt = "attempt"
    p.session = SimpleNamespace(terminate=fail, close=lambda: events.append("close"))
    monkeypatch.setattr(module, "execute", lambda *a: pytest.fail("no guessed closeout"))
    with pytest.raises(TimeoutError):
        p.destroy("attempt")
    assert events == ["close"]
