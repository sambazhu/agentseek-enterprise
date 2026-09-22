"""替代窗口（09:00-11:00 UTC）W0 与 A 槽待审材料重建：新目录/新文件名/新 token/时间字段全重算。
approved=false / w0_accepted=false / abstain 按已收确认登记但 accepted=false 且 inventory 待 .171
补充——W0 与材料摘要本轮均为待审版，不钉最终 inventory 摘要。"""
import hashlib, json, os, secrets
from pathlib import Path

RUN = "agentseek-m3-r1-20260917"
TPL = "tpl-c4ba4bf8e2fb4668b0b7f22c"
BOOT = "acbe1772-86e3-4261-b7d2-926dc729672d"
CAND = "188a0d2cdcd9705befe916a9b850e8791b81471ca88d455a8c97d9b94df9fceb"
RUNROOT = Path("/var/lib/agentseek-m3/runs") / RUN
D = RUNROOT / "config" / "A-slot-w2-20260917"
D.mkdir(mode=0o700, parents=True, exist_ok=False)
START, END = 1789635600, 1789642800  # 09:00-11:00 UTC
CONF = {"cc-171": 1789633096, "codex-dev": 1789633339,
        "zcode-172": 1789633339, "user-zhuchunlin": 1789633616}  # 实收时刻（zcode 用 08:22:19 替代窗口重述）
def w(name, value):
    p = D / name
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as fh: fh.write(json.dumps(value, indent=2) + "\n")
    return hashlib.sha256(p.read_bytes()).hexdigest()

token = "m3r1a2-" + secrets.token_hex(16)
request = {"templateID": TPL, "metadata": {"agentseek_run_id": RUN, "agentseek_create_token": token},
           "network": {"allowPublicTraffic": False}}
d_request = w("request-A-w2.json", request)
plan = {"run_id": RUN, "create_token": token, "template_id": TPL, "boot_id": BOOT,
        "candidate_sha256": CAND, "request_sha256": d_request, "domain": "cube.app",
        "restricted": True, "endpoint": "https://192.10.50.172:13000"}
d_approval = w("approval-A-PENDING-w2.json", {"schema": 1, "plan": plan, "approved": False,
               "max_creates": 1, "w0_accepted": False, "expires_epoch": END})
writers = [
    {"id": "user-zhuchunlin", "responsible": "用户（user-approver+zhuchunlin 同一主体两角色，本人声明合并）",
     "confirmed_epoch": CONF["user-zhuchunlin"], "abstain": True,
     "note": "abstain=true 依据 08:26:56Z 实收本人声明（覆盖替代窗口）；accepted 仍由用户审阅后定"},
    {"id": "codex-dev", "responsible": "Codex（开发端）", "confirmed_epoch": CONF["codex-dev"], "abstain": True,
     "note": "08:22:19Z 实收（声明 08:18:33Z）"},
    {"id": "cc-171", "responsible": ".171 Linux CC（含 SSH22589/.171 侧 key 副本两通道披露）",
     "confirmed_epoch": CONF["cc-171"], "abstain": True, "note": "08:18:16Z 实收（声明 08:13:05Z）"},
    {"id": "zcode-172", "responsible": ".172 zcode（独占执行端）", "confirmed_epoch": CONF["zcode-172"],
     "abstain": True, "note": "08:22:19Z 替代窗口重述（原持续声明 07:44Z）"},
]
w0 = {"schema": 1, "window_id": "w0-m3r1-a-w2-20260917", "run_id": RUN, "boot_id": BOOT,
      "candidate_sha256": CAND, "creator_id": "zhuchunlin",
      "inventory_sha256": None,   # 待 .171 补充盘点并入后计算钉住；本轮不钉最终摘要
      "inventory_status": "PRELIMINARY——含 4 已确认主体；.171 有界只读盘点结果（经用户转交）并入后方可定稿钉摘要",
      "accepted": False, "revoked": False, "starts_epoch": START, "ends_epoch": END,
      "writers": writers,
      "declaration_coverage_note": "平台其他有效 key/口令/瞬态持有者：以责任人声明+明确 inventory+已收禁创确认为本轮审阅依据；保留'无法从执行机独立技术排除'限制，不记技术排除 PASS；冲突事实出现即停"}
d_w0 = w("W0-window-PENDING-w2.json", w0)
pins = json.loads(Path("/etc/agentseek-m3/supervisor-identity-pins.json").read_text())["pins"]
tpl_pins = {"template_id": TPL, "node_ip": "192.10.50.172", "artifact_id": "rfs-b5457edef749be99334af6d9",
            "artifact_sha256": "b9e6915560fbaac9885f912adb8f48ea1ee75d3f8313d34d7216b2590af4a412"}
pre = {"schema": 1, "approved": False, "materialize_exact_rows": False, "expires_epoch": END,
       "slot": "A", "reserve_seconds": 5,
       "expected_create": None, "create_result_file": None,
       "receipt_source": {"vault_directory": str(RUNROOT / "vault"), "vault_key_file": str(RUNROOT / "vault" / "receipt.key"),
                          "api_key_file": str(RUNROOT / "config" / "api_key"), "domain": "cube.app", "proxy_port": 13080},
       "installation": {"supervisor_directory": str(RUNROOT / "supervisor"), "supervisor_identity": pins,
           "control": {"endpoint": "https://192.10.50.172:13000", "domain": "cube.app", "proxy_port": 13080,
                       "api_key": None, "ca_file": "/root/m3-r1-creds-20260917/ca.pem"},
           "plan": plan, "template_pins": tpl_pins,
           "exclusive_window": {"path": str(D / "W0-window-PENDING-w2.json"), "digest": None,
                                "creator_id": "zhuchunlin", "writers": [x["id"] for x in writers]},
           "create_request_file": str(D / "request-A-w2.json")},
       "output_directory": str(D / "out-A"), "dispatch_directory": str(D / "dispatch-A"),
       "case_ids": [f"A-{i}" for i in range(5)], "approval_refs": [f"appr-A-{i}" for i in range(5)]}
d_pre = w("materialize-preapproval-A-TEMPLATE-PENDING-w2.json", pre)
matrix = {"slot": "A", "reserve_seconds": 5, "guest_hard_deadline_s": 120,
          "window": {"start_utc": "2026-09-17T09:00:00Z", "end_utc": "2026-09-17T11:00:00Z",
                     "start_epoch": START, "end_epoch": END},
          "rows": [
              {"order": 0, "endpoint": "E1", "state": "correct", "case_id": "A-0", "approval_ref": "appr-A-0", "expect": "200+command_success"},
              {"order": 1, "endpoint": "E2", "state": "correct", "case_id": "A-1", "approval_ref": "appr-A-1", "expect": "200+file_payload"},
              {"order": 2, "endpoint": "E2", "state": "wrong", "case_id": "A-2", "approval_ref": "appr-A-2", "expect": "403+http_denial_signal+activity=false"},
              {"order": 3, "endpoint": "E3", "state": "correct", "case_id": "A-3", "approval_ref": "appr-A-3", "expect": "200+stat_payload"},
              {"order": 4, "endpoint": "E3", "state": "missing", "case_id": "A-4", "approval_ref": "appr-A-4", "expect": "403+http_denial_signal+activity=false"}],
          "budget": {"rows_total": 5, "slot_budget_s": 80, "reserve_s": 5, "first_row_check_remaining_s": 85,
                     "note": "创建/登记/材料生成前置耗时另计；11s 单项门槛不替代整槽判断；120s 不延长"}}
w("matrix-A-w2.json", matrix)
print(json.dumps({"dir": str(D), "digests": {"request_w2": d_request, "approval_PENDING_w2": d_approval,
              "w0_PENDING_w2": d_w0, "preapproval_TEMPLATE_PENDING_w2": d_pre}}, indent=2))
print("W2_MATERIALS_PREPARED_PENDING")
