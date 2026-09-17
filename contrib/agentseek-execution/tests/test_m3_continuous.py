import hashlib
import json
from types import SimpleNamespace
from dataclasses import asdict, replace

import pytest

from agentseek_execution import m3_continuous as module
from agentseek_execution.models import ContractError
from test_m3_receipt_probe import setup


@pytest.fixture
def chain(setup, monkeypatch):
    root = setup.root
    monkeypatch.setattr(module, "sys", SimpleNamespace(platform="linux"))
    monkeypatch.setattr(module, "os", SimpleNamespace(getuid=lambda: 0, geteuid=lambda: 0,
        close=module.os.close, listdir=module.os.listdir))
    def write(name, value):
        path = root / name
        path.write_text(json.dumps(value))
        path.chmod(0o600)
        return str(path), hashlib.sha256(path.read_bytes()).hexdigest()
    dirs = {}
    for name in ("continuous", "life", "output", "rows"):
        dirs[name] = root / name
        dirs[name].mkdir(mode=0o700)
    life = dict(slot="A", directory=str(dirs["life"]))
    permit = dict(slot="A", approved=True, materialize_exact_rows=True,
        create_result_file=str(dirs["life"] / "create-result.json"),
        output_directory=str(dirs["output"]), dispatch_directory=str(dirs["rows"]))
    expected = asdict(replace(setup.source.create, intent_sha256="0" * 64))
    plan = {k: v for k, v in expected.items() if k not in {"intent_sha256", "approval_sha256"}}
    plan["endpoint"] = "https://example.invalid"
    base = dict(plan=plan, supervisor_directory=str(root / "supervisor"),
                supervisor_identity={}, exclusive_window={}, template_pins={})
    pre, pre_sha = write("precreate.json", dict(base, api_key_file=str(setup.source.api_key_file)))
    installed = dict(schema=2, precreate_file=pre, precreate_sha256=pre_sha,
                     approval_sha256=expected["approval_sha256"], request_file=str(root / "request"),
                     vault_directory=str(setup.source.vault_directory),
                     receipt_key_file=str(setup.source.vault_key_file))
    ip, ih = write("installed.json", installed)
    lp0, lh = write("launcher.json", dict(create_file=ip, create_sha256=ih))
    life.update(launcher=lp0, launcher_sha256=lh)
    permit.update(expected_create=expected, installation=dict(base, create_request_file=installed["request_file"]),
                  receipt_source=dict(vault_directory=installed["vault_directory"],
                    vault_key_file=installed["receipt_key_file"], api_key_file=str(setup.source.api_key_file)))
    lp, ls = write("life.json", life)
    pp, ps = write("permit.json", permit)
    config = dict(schema=1, lifecycle=lp, lifecycle_sha256=ls,
                  preapproval=pp, preapproval_sha256=ps, directory=str(dirs["continuous"]))
    cp, cs = write("chain.json", config)
    calls = []
    def create(*args):
        calls.append("create")
        return dict(saved=True, next_create_authorized=False)
    def generate(*args):
        calls.append("materialize")
        sp, ss = write("output/slot.json", {"synthetic": True})
        result = dict(preapproval_sha256=ps, rows=5, next_create_authorized=False,
                      slot_file=sp, slot_sha256=ss)
        write("output/materialize-result.json", result)
        return result
    def slot(*args):
        calls.append("slot")
        return dict(slot="A", rows_observed=5, next_create_authorized=False)
    monkeypatch.setattr(module, "execute", create)
    monkeypatch.setattr(module, "materialize", generate)
    monkeypatch.setattr(module, "run_slot", slot)
    return SimpleNamespace(path=module._path(cp), sha=cs, calls=calls, dirs=dirs,
                           write=write, permit=permit, config=config)


def test_continuous_order_and_replay_denied(chain):
    s = chain
    result = module.run(s.path, s.sha)
    assert s.calls == ["create", "materialize", "slot"]
    assert result["closeout_required"] is True
    assert result["next_create_authorized"] is False
    with pytest.raises(ContractError):
        module.run(s.path, s.sha)
    assert s.calls == ["create", "materialize", "slot"]


@pytest.mark.parametrize("stage", ["execute", "materialize", "run_slot"])
def test_failure_stops_chain_and_burns_intent(chain, monkeypatch, stage):
    def fail(*args):
        raise RuntimeError("synthetic unknown")
    monkeypatch.setattr(module, stage, fail)
    with pytest.raises(RuntimeError):
        module.run(chain.path, chain.sha)
    expected = {"execute": [], "materialize": ["create"], "run_slot": ["create", "materialize"]}
    assert chain.calls == expected[stage]
    assert (chain.dirs["continuous"] / "continuous-intent.json").exists()
    assert not (chain.dirs["continuous"] / "continuous-result.json").exists()
    with pytest.raises(ContractError):
        module.run(chain.path, chain.sha)


@pytest.mark.parametrize("change", ["b", "unapproved", "foreign_result", "alias", "foreign_binding"])
def test_preflight_rejects_without_create(chain, change):
    s = chain
    if change == "b":
        s.permit["slot"] = "B"
    elif change == "unapproved":
        s.permit["approved"] = False
    elif change == "foreign_result":
        s.permit["create_result_file"] = str(s.dirs["output"] / "wrong.json")
    elif change == "alias":
        s.permit["dispatch_directory"] = str(s.dirs["life"])
    else:
        s.permit["expected_create"]["create_token"] = "foreign"
    _, sha = s.write("permit.json", s.permit)
    s.config["preapproval_sha256"] = sha
    _, sha = s.write("chain.json", s.config)
    with pytest.raises(ContractError):
        module.run(s.path, sha)
    assert s.calls == []


def test_changed_preapproval_after_create_stops_before_generation(chain, monkeypatch):
    original = module.execute
    def changed(*args):
        result = original(*args)
        chain.permit["approved"] = False
        chain.write("permit.json", chain.permit)
        return result
    monkeypatch.setattr(module, "execute", changed)
    with pytest.raises(ContractError):
        module.run(chain.path, chain.sha)
    assert chain.calls == ["create"]


@pytest.mark.parametrize("mode", ["missing_manifest", "tampered_slot", "wrong_rows"])
def test_incomplete_generation_never_dispatches(chain, monkeypatch, mode):
    original = module.materialize
    def changed(*args):
        result = original(*args)
        if mode == "missing_manifest":
            (chain.dirs["output"] / "materialize-result.json").unlink()
        elif mode == "tampered_slot":
            chain.write("output/slot.json", {"tampered": True})
        else:
            result["rows"] = 4
            chain.write("output/materialize-result.json", result)
        return result
    monkeypatch.setattr(module, "materialize", changed)
    with pytest.raises((OSError, ContractError)):
        module.run(chain.path, chain.sha)
    assert chain.calls == ["create", "materialize"]


def test_summary_write_failure_never_replays(chain, monkeypatch):
    original = module._save
    def fail(fd, name, value):
        if name == "continuous-result.json":
            raise OSError("synthetic disk failure")
        return original(fd, name, value)
    monkeypatch.setattr(module, "_save", fail)
    with pytest.raises(OSError):
        module.run(chain.path, chain.sha)
    with pytest.raises(ContractError):
        module.run(chain.path, chain.sha)
    assert chain.calls == ["create", "materialize", "slot"]
