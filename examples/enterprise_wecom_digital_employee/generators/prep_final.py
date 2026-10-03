"""§二.3-§二.5 准备：全文件摘要重读核验、生成 materialize 最终预批（仅翻双布尔）、
建 out-A/dispatch-A 空目录、写私有执行记录。零执行入口调用。"""
import hashlib, json, os
from pathlib import Path
ROOT = Path("/var/lib/agentseek-m3/runs/agentseek-m3-r1-20260917")
D = ROOT / "config" / "A-slot-w2-20260917"; CH = D / "chain"
def sha(p): return hashlib.sha256(Path(p).read_bytes()).hexdigest()
files = {
    "W0_FINAL": D / "W0-window-FINAL-w2.json",
    "approval_FINAL": D / "approval-A-FINAL-w2.json",
    "request_A": D / "request-A-w2.json",
    "precreate": CH / "precreate-A-PENDING-w2.json",
    "create_installation": CH / "create-installation-A-PENDING-w2.json",
    "launcher": CH / "launcher-A-PENDING-w2.json",
    "lifecycle": CH / "lifecycle-A-PENDING-w2.json",
    "request_B": CH / "request-B-PENDING-w2.json",
    "preapproval_PENDING": D / "materialize-preapproval-A-COMPLETE-PENDING-w2.json",
}
dig = {k: sha(v) for k, v in files.items()}
# 引用完整性（从文件内容验证，不信任外部记录）
w0 = json.loads(files["W0_FINAL"].read_text()); ap = json.loads(files["approval_FINAL"].read_text())
pre = json.loads(files["precreate"].read_text()); inst = json.loads(files["create_installation"].read_text())
lau = json.loads(files["launcher"].read_text()); lc = json.loads(files["lifecycle"].read_text())
pend = json.loads(files["preapproval_PENDING"].read_text()); reqB = json.loads(files["request_B"].read_text())
checks = {
    "inst.approval_sha256==sha(approval_FINAL)": inst["approval_sha256"] == dig["approval_FINAL"],
    "inst.precreate_sha256==sha(precreate)": inst["precreate_sha256"] == dig["precreate"],
    "planA.request_sha256==sha(request_A)": ap["plan"]["request_sha256"] == dig["request_A"],
    "pre.exclusive_window.digest==sha(W0_FINAL)": pre["exclusive_window"]["digest"] == dig["W0_FINAL"],
    "lau.create_sha256==sha(inst)": lau["create_sha256"] == dig["create_installation"],
    "lc.launcher_sha256==sha(lau)": lc["launcher_sha256"] == dig["launcher"],
    "pend.expected_create.approval_sha256==sha(approval_FINAL)": pend["expected_create"]["approval_sha256"] == dig["approval_FINAL"],
    "pend.exclusive_window.digest==sha(W0_FINAL)": pend["installation"]["exclusive_window"]["digest"] == dig["W0_FINAL"],
    "inst.batch[1].request_sha256==sha(request_B)": inst["batch_sequence"][1]["request_sha256"] == dig["request_B"],
    "pend.intent_sha256==64零": pend["expected_create"]["intent_sha256"] == "0" * 64,
    "W0 accepted/全 abstain/confirmed<=start": w0["accepted"] is True and all(x["abstain"] is True and x["confirmed_epoch"] <= 1789635600 for x in w0["writers"]),
    "approval 双 true": ap["approved"] is True and ap["w0_accepted"] is True,
}
assert all(checks.values()), checks
# 生成最终预批：仅翻双布尔
final = json.loads(json.dumps(pend))
final["approved"] = True; final["materialize_exact_rows"] = True
diff = {k for k in set(pend) | set(final) if pend.get(k) != final.get(k)}
assert diff == {"approved", "materialize_exact_rows"}, diff
fp = D / "materialize-preapproval-A-FINAL-w2.json"
fd = os.open(fp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
with os.fdopen(fd, "w") as fh: fh.write(json.dumps(final, indent=2) + "\n")
dig["preapproval_FINAL"] = sha(fp)
for sub in ("out-A", "dispatch-A"):
    (D / sub).mkdir(mode=0o700, exist_ok=False)
rec = {"generated_utc": "2026-09-17T08:5x:xxZ-window-pending", "window": [1789635600, 1789642800],
       "materialize_final_preapproval": str(fp), "digests": dig, "reference_checks": "12/12 PASS",
       "boolean_diff_only": sorted(diff)}
rp = D / "EXECUTION-RECORD-A.json"
rp.write_text(json.dumps(rec, indent=2) + "\n"); os.chmod(rp, 0o600)
print(json.dumps({"digests": dig, "checks_12": "ALL PASS", "diff_only": sorted(diff)}, indent=2))
print("PREP_DONE")
