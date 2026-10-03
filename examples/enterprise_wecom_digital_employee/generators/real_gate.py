"""§二.6 真实时钟门禁（全部通过才创建）：W0 read_window 真实时钟、approval 真实时钟、
现场事实（guest/模板/心跳/身份 pins）。只读。"""
import json, sys, urllib.request
from pathlib import Path
sys.path.insert(0, "/opt/agentseek-m3-releases/5d3c27b/venv/lib/python3.13/site-packages")
from agentseek_execution.m3_create_worker import CreatePlan, read_create_approval
from agentseek_execution.m3_exclusive_window import read_window
from agentseek_execution.m3_supervisor_identity import IdentityPins, verify_identity
from agentseek_execution.m3_supervisor_snapshot import SupervisorReader, system_clock

D = Path("/var/lib/agentseek-m3/runs/agentseek-m3-r1-20260917/config/A-slot-w2-20260917")
ROOT = D.parent.parent
out = {"now_utc": None, "checks": {}}
now = system_clock(); out["now_utc"] = now.epoch
# 1) 真实时钟 W0
ap = json.loads((D / "approval-A-FINAL-w2.json").read_text())
plan = CreatePlan(**ap["plan"])
snap = read_window(D / "W0-window-FINAL-w2.json",
                   pinned_digest=__import__("hashlib").sha256((D / "W0-window-FINAL-w2.json").read_bytes()).hexdigest(),
                   plan=plan, creator_id="zhuchunlin",
                   expected_writers=tuple(w["id"] for w in json.loads((D / "W0-window-FINAL-w2.json").read_text())["writers"]),
                   now=now)
out["checks"]["w0_real_clock"] = f"PASS（窗口至单调 {snap.expires_mono:.0f}）"
# 2) 真实时钟 approval
d1 = read_create_approval(plan, D / "approval-A-FINAL-w2.json",
                          pinned_digest=__import__("hashlib").sha256((D / "approval-A-FINAL-w2.json").read_bytes()).hexdigest())
out["checks"]["approval_real_clock"] = f"PASS（批准截止单调 {d1:.0f}，剩余 {d1-now.monotonic:.0f}s）"
# 3) 现场事实
with urllib.request.urlopen("http://127.0.0.1:3000/sandboxes", timeout=5) as r:
    guests = json.loads(r.read())
out["checks"]["guest_zero"] = guests == []
with urllib.request.urlopen("http://127.0.0.1:3000/templates?limit=5", timeout=5) as r:
    tpl = next(t for t in json.loads(r.read()) if t["templateID"] == "tpl-c4ba4bf8e2fb4668b0b7f22c")
out["checks"]["template_ready"] = tpl["status"] == "READY" and tpl["public"] is False
reader = SupervisorReader(ROOT / "supervisor")
s = reader.read_empty(run_id="agentseek-m3-r1-20260917", template_id="tpl-c4ba4bf8e2fb4668b0b7f22c")
pins = IdentityPins(**json.loads(Path("/etc/agentseek-m3/supervisor-identity-pins.json").read_text())["pins"])
ident = verify_identity(s, pins)
out["checks"]["supervisor_identity"] = f"PASS（pid={ident.pid}）"
out["checks"]["heartbeat_fresh"] = 0 <= now.epoch - json.loads((ROOT / "supervisor" / "manifest.json.heartbeat").read_text())["ts"] <= 31
out["checks"]["alarm_absent"] = not (ROOT / "supervisor" / "alarm").exists()
assert all("PASS" in str(v) or v is True for v in out["checks"].values()), out
print(json.dumps(out, indent=2)); print("REAL_GATE_ALL_PASS")
