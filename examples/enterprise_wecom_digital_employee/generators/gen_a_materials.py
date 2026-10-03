"""A 槽审批材料准备（待审版，全部不可执行）：
request / approval-PENDING / W0-PENDING / materialize 预批模板-PENDING / 矩阵与核对说明。
不写 approved=true、不写 w0_accepted=true、不写 abstain=true、不入 api_key 正文、不调用任何执行入口。"""
import hashlib, json, os, secrets
from pathlib import Path

RUN = "agentseek-m3-r1-20260917"
TPL = "tpl-c4ba4bf8e2fb4668b0b7f22c"
BOOT = "acbe1772-86e3-4261-b7d2-926dc729672d"
CAND = "188a0d2cdcd9705befe916a9b850e8791b81471ca88d455a8c97d9b94df9fceb"  # 5d3c27b wheel
RUNROOT = Path("/var/lib/agentseek-m3/runs") / RUN
D = RUNROOT / "config" / "A-slot-20260917"
D.mkdir(mode=0o700, parents=True, exist_ok=False)
START, END = 1789630800, 1789639200  # 2026-09-17 07:40–10:00 UTC（15:40–18:00 Asia/Shanghai）

def canon(v): return json.dumps(v, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
def sha_doc(v): return hashlib.sha256(canon(v).encode()).hexdigest()
def w(name, value):
    p = D / name
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as fh: fh.write(json.dumps(value, indent=2) + "\n")
    return hashlib.sha256(p.read_bytes()).hexdigest()

token = "m3r1-a-" + secrets.token_hex(16)
request = {"templateID": TPL,
           "metadata": {"agentseek_run_id": RUN, "agentseek_create_token": token},
           "network": {"allowPublicTraffic": False}}
d_request = w("request-A.json", request)
plan = {"run_id": RUN, "create_token": token, "template_id": TPL, "boot_id": BOOT,
        "candidate_sha256": CAND, "request_sha256": d_request, "domain": "cube.app",
        "restricted": True, "endpoint": "https://192.10.50.172:13000"}
approval = {"schema": 1, "plan": plan, "approved": False, "max_creates": 1,
            "w0_accepted": False, "expires_epoch": END}
d_approval = w("approval-A-PENDING.json", approval)
writers = [
    {"id": "user-approver", "responsible": "用户（批准点）", "confirmed_epoch": None, "abstain": False},
    {"id": "zhuchunlin", "responsible": "zhuchunlin（W0 责任人，待确认入口清单与其本人禁创）", "confirmed_epoch": None, "abstain": False},
    {"id": "codex-dev", "responsible": "Codex（开发端，无直接平台通道，经工单链）", "confirmed_epoch": None, "abstain": False},
    {"id": "cc-171", "responsible": ".171 Linux CC（SSH 只读；历史备料曾接触控制面材料）", "confirmed_epoch": None, "abstain": False},
    {"id": "zcode-172", "responsible": ".172 zcode（本机独占执行端）", "confirmed_epoch": None, "abstain": False},
    {"id": "unknown-root-or-key-holders", "responsible": "未知 root/API key 持有者（BLOCKED：无法从本机核实）", "confirmed_epoch": None, "abstain": False},
]
w0 = {"schema": 1, "window_id": "w0-m3r1-a-20260917", "run_id": RUN, "boot_id": BOOT,
      "candidate_sha256": CAND, "creator_id": "zhuchunlin", "inventory_sha256": None,
      "accepted": False, "revoked": False, "starts_epoch": START, "ends_epoch": END,
      "writers": writers}
d_w0 = w("W0-window-PENDING.json", w0)
pins = json.loads(Path("/etc/agentseek-m3/supervisor-identity-pins.json").read_text())["pins"]
tpl_pins = {"template_id": TPL, "node_ip": "192.10.50.172",
            "artifact_id": "rfs-b5457edef749be99334af6d9", "artifact_sha256": "b9e6915560fbaac9885f912adb8f48ea1ee75d3f8313d34d7216b2590af4a412"}
pre = {"schema": 1, "approved": False, "materialize_exact_rows": False, "expires_epoch": END,
       "slot": "A", "reserve_seconds": 5,
       "expected_create": None, "create_result_file": None,   # 动态：待 A 真实回执
       "receipt_source": {"vault_directory": str(RUNROOT / "vault"),
                          "vault_key_file": str(RUNROOT / "vault" / "receipt.key"),
                          "api_key_file": str(RUNROOT / "config" / "api_key"),
                          "domain": "cube.app", "proxy_port": 13080},
       "installation": {"supervisor_directory": str(RUNROOT / "supervisor"),
                        "supervisor_identity": pins,
                        "control": {"endpoint": "https://192.10.50.172:13000", "domain": "cube.app",
                                    "proxy_port": 13080, "api_key": None, "ca_file": "/root/m3-r1-creds-20260917/ca.pem"},
                        "plan": plan, "template_pins": tpl_pins,
                        "exclusive_window": {"path": str(D / "W0-window-PENDING.json"), "digest": None,
                                             "creator_id": "zhuchunlin", "writers": [x["id"] for x in writers]},
                        "create_request_file": str(D / "request-A.json")},
       "output_directory": str(RUNROOT / "config" / "A-slot-20260917" / "out-A"),
       "dispatch_directory": str(RUNROOT / "config" / "A-slot-20260917" / "dispatch-A"),
       "case_ids": [f"A-{i}" for i in range(5)], "approval_refs": [f"appr-A-{i}" for i in range(5)]}
d_pre = w("materialize-preapproval-A-TEMPLATE-PENDING.json", pre)
matrix = {"slot": "A", "reserve_seconds": 5, "guest_hard_deadline_s": 120,
          "window": {"start_utc": "2026-09-17T07:40:00Z", "end_utc": "2026-09-17T10:00:00Z"},
          "rows": [
              {"order": 0, "endpoint": "E1", "state": "correct", "case_id": "A-0", "approval_ref": "appr-A-0", "expect": "200+command_success"},
              {"order": 1, "endpoint": "E2", "state": "correct", "case_id": "A-1", "approval_ref": "appr-A-1", "expect": "200+file_payload"},
              {"order": 2, "endpoint": "E2", "state": "wrong", "case_id": "A-2", "approval_ref": "appr-A-2", "expect": "403+http_denial_signal+activity=false"},
              {"order": 3, "endpoint": "E3", "state": "correct", "case_id": "A-3", "approval_ref": "appr-A-3", "expect": "200+stat_payload"},
              {"order": 4, "endpoint": "E3", "state": "missing", "case_id": "A-4", "approval_ref": "appr-A-4", "expect": "403+http_denial_signal+activity=false"}],
          "budget": {"rows_total": 5, "slot_budget_s": 80, "reserve_s": 5, "first_row_check_remaining_s": 85}}
w("matrix-A.json", matrix)
print(json.dumps({"files": sorted(p.name for p in D.iterdir()),
                  "digests": {"request": d_request, "approval_PENDING": d_approval,
                              "w0_PENDING": d_w0, "preapproval_TEMPLATE_PENDING": d_pre},
                  "token_len": len(token)}, indent=2))
print("A_MATERIALS_PREPARED_PENDING")
