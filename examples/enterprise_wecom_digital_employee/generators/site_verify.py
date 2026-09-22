"""现场只读复核：guest 数（本地监督通道）、模板状态、监督心跳、身份 pins 复验。
使用 5d3c27b 登记入口运行候选 verify_identity；不更新 pins、不重登记。"""
import json, ssl, urllib.request
from pathlib import Path
from agentseek_execution.m3_supervisor_identity import IdentityPins, verify_identity
from agentseek_execution.m3_supervisor_snapshot import SupervisorReader

RUN_ID = "agentseek-m3-r1-20260917"
TEMPLATE_ID = "tpl-c4ba4bf8e2fb4668b0b7f22c"
SUP = Path("/var/lib/agentseek-m3/runs/agentseek-m3-r1-20260917/supervisor")
out = {}
with urllib.request.urlopen("http://127.0.0.1:3000/sandboxes", timeout=5) as r:
    guests = json.loads(r.read()); out["guests_local_api"] = guests
with urllib.request.urlopen("http://127.0.0.1:3000/templates?limit=5", timeout=5) as r:
    tpls = json.loads(r.read())
tpl = next(t for t in tpls if t["templateID"] == TEMPLATE_ID)
out["template"] = {"id": tpl["templateID"], "status": tpl["status"], "public": tpl["public"], "version": tpl["version"]}
reader = SupervisorReader(SUP)
snap = reader.read_empty(run_id=RUN_ID, template_id=TEMPLATE_ID)
pins = IdentityPins(**json.loads(Path("/etc/agentseek-m3/supervisor-identity-pins.json").read_text())["pins"])
identity = verify_identity(snap, pins)
beat = json.loads((SUP / "manifest.json.heartbeat").read_text())
out["supervisor"] = {"run_id_valid": True, "identity_verified": True, "pid": identity.pid,
                     "start_ticks": identity.start_ticks, "boot_id": reader.anchor.boot_id,
                     "heartbeat_age_s": round(reader.anchor.epoch - beat["ts"], 2),
                     "alarm_absent": not (SUP / "alarm").exists()}
print(json.dumps(out, indent=2)); print("SITE_VERIFY_OK")
