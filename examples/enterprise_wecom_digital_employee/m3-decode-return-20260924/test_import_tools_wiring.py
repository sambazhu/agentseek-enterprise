from pathlib import Path
VENV = '/root/m3-decode-scratch-20260924/venv/lib/python3.13/site-packages'
TOOLS = '/root/m3-decode-handoff-20260924/tools'

def test_materials_tools_from_delivered_tools_and_exec_from_venv():
    import business_materials, business_materials_input, agentseek_execution
    assert Path(business_materials.__file__).is_relative_to(TOOLS), business_materials.__file__
    assert Path(business_materials_input.__file__).is_relative_to(TOOLS), business_materials_input.__file__
    assert Path(agentseek_execution.__file__).is_relative_to(VENV), agentseek_execution.__file__
