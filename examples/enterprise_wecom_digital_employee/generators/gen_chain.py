"""A 创建安装链待审件生成 + materialize 预批完整待审版（全部 PENDING，布尔 false）。
引用摘要一律从实际文件读取；生成后离线结构核验；零执行入口调用。"""
import hashlib, json, os, secrets, sys
from pathlib import Path
sys.path.insert(0, "/opt/agentseek-m3-releases/5d3c27b/venv/lib/python3.13/site-packages")
from agentseek_execution.m3_create_worker import CreatePlan
from agentseek_execution.m3_create_receipt import CreateBinding

RUN = "agentseek-m3-r1-20260917"
TPL = "tpl-c4ba4bf8e2fb4668b0b7f22c"
BOOT = "acbe1772-86e3-4261-b7d2-926dc729672d"
ROOT = Path("/var/lib/agentseek-m3/runs") / RUN
D = ROOT / "config" / "A-slot-w2-20260917"
CH = D / "chain"; LC = D / "lifecycle-A"
CH.mkdir(mode=0o700, exist_ok=False); LC.mkdir(mode=0o700, exist_ok=False)
def sha(p): return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def w(p, v):
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as fh: fh.write(json.dumps(v, indent=2) + "\n")
    return sha(p)

d_apf = sha(D / "approval-A-FINAL-w2.json")
d_w0f = sha(D / "W0-window-FINAL-w2.json")
d_reqA = sha(D / "request-A-w2.json")
planA = json.loads((D / "approval-A-FINAL-w2.json").read_text())["plan"]
pins = json.loads(Path("/etc/agentseek-m3/supervisor-identity-pins.json").read_text())["pins"]
KEY_SHA = sha(ROOT / "config" / "api_key"); KEY = (ROOT / "config" / "api_key").read_text()
CA_SHA = sha("/root/m3-r1-creds-20260917/ca.pem")
SCRIPT_SHA = sha("/opt/agentseek-m3-supervisor/node_supervisor.py")
CREATE_RESULT_PATH = str(LC / "create-result.json")  # 约定输出路径（lifecycle create 落盘处）

# 1) B 固定计划（登记≠授权）：新 token + 新 request 待审件
tokenB = "m3r1b-" + secrets.token_hex(16)
reqB = {"templateID": TPL, "metadata": {"agentseek_run_id": RUN, "agentseek_create_token": tokenB},
        "network": {"allowPublicTraffic": False}}
d_reqB = w(CH / "request-B-PENDING-w2.json", reqB)
planB = dict(planA); planB["create_token"] = tokenB; planB["request_sha256"] = d_reqB

# 2) precreate（schema-3，引用 approval-FINAL 与 W0-FINAL）
precreate = {"schema": 3, "plan": planA, "candidate_sha256": planA["candidate_sha256"],
             "approval_file": str(D / "approval-A-FINAL-w2.json"),
             "supervisor_directory": str(ROOT / "supervisor"), "supervisor_identity": pins,
             "api_key_file": str(ROOT / "config" / "api_key"),
             "ca_file": "/root/m3-r1-creds-20260917/ca.pem",
             "template_pins": {"template_id": TPL, "node_ip": "192.10.50.172",
                               "artifact_id": "rfs-b5457edef749be99334af6d9",
                               "artifact_sha256": "b9e6915560fbaac9885f912adb8f48ea1ee75d3f8313d34d7216b2590af4a412"},
             "exclusive_window": {"path": str(D / "W0-window-FINAL-w2.json"), "digest": d_w0f,
                                  "creator_id": "zhuchunlin",
                                  "writers": ["cc-171", "codex-dev", "user-zhuchunlin", "zcode-172"]}}
d_pre = w(CH / "precreate-A-PENDING-w2.json", precreate)

# 3) create installation（schema-2，batch_sequence=[A,B]）
inst = {"schema": 2, "precreate_file": str(CH / "precreate-A-PENDING-w2.json"), "precreate_sha256": d_pre,
        "approval_sha256": d_apf, "request_file": str(D / "request-A-w2.json"),
        "vault_directory": str(ROOT / "vault"), "fence_directory": str(ROOT / "fence"),
        "receipt_key_file": str(ROOT / "vault" / "receipt.key"),
        "receipt_key_sha256": sha(ROOT / "vault" / "receipt.key"),
        "api_key_sha256": KEY_SHA, "ca_sha256": CA_SHA,
        "batch_sequence": [planA, planB], "quota_directory": str(ROOT / "quota")}
d_inst = w(CH / "create-installation-A-PENDING-w2.json", inst)

# 4) launcher（schema-1，tracking-A）
launcher = {"schema": 1, "create_file": str(CH / "create-installation-A-PENDING-w2.json"),
            "create_sha256": d_inst, "candidate_sha256": planA["candidate_sha256"],
            "supervisor_script": "/opt/agentseek-m3-supervisor/node_supervisor.py",
            "supervisor_sha256": SCRIPT_SHA, "tracking_directory": str(ROOT / "tracking-A")}
d_lau = w(CH / "launcher-A-PENDING-w2.json", launcher)

# 5) lifecycle（schema-1，A 无 previous；directory=lifecycle-A）
lifecycle = {"schema": 1, "slot": "A", "launcher": str(CH / "launcher-A-PENDING-w2.json"),
             "launcher_sha256": d_lau, "directory": str(LC)}
d_lc = w(CH / "lifecycle-A-PENDING-w2.json", lifecycle)

# 6) materialize 预批完整待审版（intent 全零占位、create_result_file 固定约定路径、control 填真 key；布尔 false）
expected = CreatePlan(**planA).binding(d_apf)
assert expected.intent_sha256 == "0" * 64
pre = {"schema": 1, "approved": False, "materialize_exact_rows": False, "expires_epoch": 1789642800,
       "slot": "A", "reserve_seconds": 5, "expected_create": json.loads(json.dumps(vars(expected))),
       "create_result_file": CREATE_RESULT_PATH,
       "receipt_source": {"vault_directory": str(ROOT / "vault"), "vault_key_file": str(ROOT / "vault" / "receipt.key"),
                          "api_key_file": str(ROOT / "config" / "api_key"), "domain": "cube.app", "proxy_port": 13080},
       "installation": dict(precreate["installation"] if "installation" in precreate else {},
                            supervisor_directory=str(ROOT / "supervisor"), supervisor_identity=pins,
                            control={"endpoint": "https://192.10.50.172:13000", "domain": "cube.app",
                                     "proxy_port": 13080, "api_key": KEY, "ca_file": "/root/m3-r1-creds-20260917/ca.pem"},
                            plan=planA, template_pins=precreate["template_pins"],
                            exclusive_window=precreate["exclusive_window"],
                            create_request_file=str(D / "request-A-w2.json")),
       "output_directory": str(D / "out-A"), "dispatch_directory": str(D / "dispatch-A"),
       "case_ids": [f"A-{i}" for i in range(5)], "approval_refs": [f"appr-A-{i}" for i in range(5)]}
d_pre_complete = w(D / "materialize-preapproval-A-COMPLETE-PENDING-w2.json", pre)

# ---- 离线结构核验 ----
v = {}
v["lifecycle 字段集"] = set(json.loads((CH/"lifecycle-A-PENDING-w2.json").read_text())) == {"schema","slot","launcher","launcher_sha256","directory"}
v["launcher 字段集"] = set(json.loads((CH/"launcher-A-PENDING-w2.json").read_text())) == {"schema","create_file","create_sha256","candidate_sha256","supervisor_script","supervisor_sha256","tracking_directory"}
v["installation 字段集(schema2)"] = set(inst) == {"schema","precreate_file","precreate_sha256","approval_sha256","request_file","vault_directory","fence_directory","receipt_key_file","receipt_key_sha256","api_key_sha256","ca_sha256","batch_sequence","quota_directory"}
v["precreate 字段集(schema3)"] = set(precreate) == {"schema","plan","candidate_sha256","approval_file","supervisor_directory","supervisor_identity","api_key_file","ca_file","template_pins","exclusive_window"}
v["preapproval 14 字段"] = set(pre) == {"schema","approved","materialize_exact_rows","expires_epoch","slot","reserve_seconds","expected_create","create_result_file","receipt_source","installation","output_directory","dispatch_directory","case_ids","approval_refs"}
v["引用摘要一致"] = (inst["precreate_sha256"] == d_pre and launcher["create_sha256"] == d_inst
                  and lifecycle["launcher_sha256"] == d_lau and precreate["exclusive_window"]["digest"] == d_w0f
                  and inst["approval_sha256"] == d_apf and planA["request_sha256"] == d_reqA)
v["expected_create==plan.binding(approval)"] = CreatePlan(**planA).binding(d_apf) == CreateBinding(**pre["expected_create"])
v["batch_sequence A/B token 唯一"] = planA["create_token"] != planB["create_token"]
v["路径互斥"] = len({inst["vault_directory"], inst["fence_directory"], inst["quota_directory"], str(ROOT/"tracking-A"), precreate["supervisor_directory"]}) == 5
import pathlib
v["全部路径规范绝对"] = all(pathlib.Path(p).is_absolute() and pathlib.Path(p).resolve()==pathlib.Path(p) for p in
    [inst["precreate_file"], inst["request_file"], precreate["approval_file"], launcher["create_file"],
     lifecycle["launcher"], pre["create_result_file"], precreate["api_key_file"]])
assert all(v.values()), v
print(json.dumps({"digests": {"request_B": d_reqB, "precreate": d_pre, "create_installation": d_inst,
              "launcher": d_lau, "lifecycle": d_lc, "preapproval_COMPLETE": d_pre_complete},
              "verification": {k: True for k in v},
              "create_result_path": CREATE_RESULT_PATH}, indent=2))
print("CHAIN_PENDING_GENERATED_VERIFIED")
