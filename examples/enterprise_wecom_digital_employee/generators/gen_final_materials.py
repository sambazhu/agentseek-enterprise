"""生成 A 槽最终批准材料（新建文件，不翻转待审件）：
W0-FINAL（accepted=true，writer 条目按 read_window 精确四键集合）
approval-FINAL（approved=true/w0_accepted=true/max_creates=1）
request 沿用 w2 版（无布尔、其摘要被 plan 引用）。
生成后用候选读取器离线核验（read_create_approval 真实时钟；read_window 用合成窗口内时钟并注明）。"""
import hashlib, json, os, sys
from pathlib import Path
sys.path.insert(0, "/opt/agentseek-m3-releases/5d3c27b/venv/lib/python3.13/site-packages")
from agentseek_execution.m3_create_worker import CreatePlan, read_create_approval
from agentseek_execution.m3_exclusive_window import read_window
from agentseek_execution.m3_supervisor_snapshot import ClockSample

D = Path("/var/lib/agentseek-m3/runs/agentseek-m3-r1-20260917/config/A-slot-w2-20260917")
START, END = 1789635600, 1789642800
def canon(v): return json.dumps(v, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
def sha_file(p): return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def w(name, value):
    p = D / name
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as fh: fh.write(json.dumps(value, indent=2) + "\n")
    return sha_file(p)

pending_w0 = json.loads((D / "W0-window-PENDING-w2.json").read_text())
pending_ap = json.loads((D / "approval-A-PENDING-w2.json").read_text())
plan = pending_ap["plan"]  # 与待审版完全一致（含 request 摘要引用）
# 1) W0-FINAL：12 字段精确集 + writer 四键精确集（注释移台账）
w0f = {"schema": 1, "window_id": pending_w0["window_id"], "run_id": pending_w0["run_id"],
       "boot_id": pending_w0["boot_id"], "candidate_sha256": pending_w0["candidate_sha256"],
       "creator_id": "zhuchunlin",
       "inventory_sha256": hashlib.sha256(canon(sorted(x["id"] for x in pending_w0["writers"])).encode()).hexdigest(),
       "accepted": True, "revoked": False, "starts_epoch": START, "ends_epoch": END,
       "writers": [{"id": x["id"], "responsible": x["responsible"].split("（")[0],
                    "confirmed_epoch": x["confirmed_epoch"], "abstain": True} for x in pending_w0["writers"]]}
d_w0f = w("W0-window-FINAL-w2.json", w0f)
# 2) approval-FINAL
apf = dict(pending_ap); apf["approved"] = True; apf["w0_accepted"] = True
d_apf = w("approval-A-FINAL-w2.json", apf)
d_req = sha_file(D / "request-A-w2.json")

# 3) 离线合同核验
v = {}
plan_obj = CreatePlan(**plan)
snap = read_create_approval(plan_obj, D / "approval-A-FINAL-w2.json", pinned_digest=d_apf)
v["read_create_approval"] = f"PASS（真实时钟，批准截止单调剩余 {snap - __import__('time').monotonic():.0f}s）"
inside = ClockSample(boot_id=pending_w0["boot_id"], epoch=START + 60, monotonic=0, uptime=999999)
wcheck = read_window(D / "W0-window-FINAL-w2.json", pinned_digest=d_w0f, plan=plan_obj,
                     creator_id="zhuchunlin",
                     expected_writers=tuple(x["id"] for x in w0f["writers"]), now=inside)
v["read_window"] = f"PASS（合成窗口内时钟 {START+60} 验证；生产执行时由真实时钟再验 start<=now 与 end-now>=11）"
v["cross_refs"] = {
    "plan.request_sha256 == sha256(request-A-w2.json)": plan["request_sha256"] == d_req,
    "plan.candidate_sha256 == 5d3c27b wheel": plan["candidate_sha256"] == "188a0d2cdcd9705befe916a9b850e8791b81471ca88d455a8c97d9b94df9fceb",
    "w0.candidate_sha256 == plan.candidate_sha256": w0f["candidate_sha256"] == plan["candidate_sha256"],
    "w0.run_id == plan.run_id": w0f["run_id"] == plan["run_id"],
    "w0.boot_id == plan.boot_id": w0f["boot_id"] == plan["boot_id"],
    "全部 confirmed_epoch <= start": all(x["confirmed_epoch"] <= START for x in w0f["writers"]),
    "inventory_sha256 == canonical(sorted(ids)) 摘要": w0f["inventory_sha256"] == "92edc3aef4c56648ce6560a48dcde56db55725c3d7062c509c1a8f247cdb7689",
    "expires_epoch == 窗口末": apf["expires_epoch"] == END == w0f["ends_epoch"],
}
assert all(v["cross_refs"].values())
print(json.dumps({"digests": {"W0_FINAL": d_w0f, "approval_FINAL": d_apf, "request": d_req},
                  "verification": v, "window_id": w0f["window_id"],
                  "writers": [(x["id"], x["confirmed_epoch"]) for x in w0f["writers"]]}, indent=2))
print("FINAL_MATERIALS_GENERATED_AND_VERIFIED")
