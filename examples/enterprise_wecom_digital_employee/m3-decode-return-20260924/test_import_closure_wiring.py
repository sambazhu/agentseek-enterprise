from pathlib import Path
VENV = '/root/m3-decode-scratch-20260924/venv/lib/python3.13/site-packages'
EX = '/root/m3-decode-handoff-20260924/source-closure/examples'

def test_execution_pkg_from_venv_site_packages():
    import agentseek_execution, agentseek_execution.business_cube_session as bcs
    assert Path(agentseek_execution.__file__).is_relative_to(VENV), agentseek_execution.__file__
    assert Path(bcs.__file__).is_relative_to(VENV), bcs.__file__

def test_sandbox_poc_from_closure_examples_only():
    import sandbox_poc
    f = Path(sandbox_poc.__file__).resolve()
    assert f.is_relative_to(EX), f
    assert 'site-packages' not in str(f) and 'contrib' not in str(f)
