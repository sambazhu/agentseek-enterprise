# Enterprise WeCom Digital Employee — Deployment Notes (Mac mini)

Handoff notes from deploying/verifying this example on a company Mac mini
(branch `enterprise/wecom-runtime`, 2026-06-27). Covers the DM-connection
root cause, the working configuration, known-issue workarounds, and the
remaining work items.

## Verified working (end-to-end)

WeCom callback → signature verify/decrypt → `open_userid` → plaintext userid
(e.g. `zhuchunlin`) → DM identity lookup (`朱春霖 / 数智产品研发团队 / 团队长兼数据架构师`)
→ model (`glm-5.2` via DashScope) → reply. Confirmed with `我是谁 → 你好，朱春霖！...`
including full org path/role. Final verified state: FlClash in **system-proxy
mode (TUN off)** — see the FlClash section for why TUN must stay off.

## Test log (2026-06-27, Mac mini)

What was tested and the outcome, so the next session knows the current state:

- **End-to-end identity (live, WeCom):** `我是谁 → 你好，朱春霖！` with full
  org path/role. Needs both the DM lookup (employee context) and the WeCom API
  (`openuserid_to_userid`) to succeed. PASS with FlClash TUN off.
- **DM JDBC connect (probe):** `scripts/probe_staff_identity.py --oa zhuchunlin
  --source python-db` returns the full EmployeeContext (name/dept/post/org).
  PASS with FlClash TUN off (or TUN on + the `/32` route).
- **WeCom retry dedup (live):** a slow-reply message produces `1 × msgtype=text`
  + `N × msgtype=stream` (polls); 1 agent turn, 1 reply, no flood.
  `wecom.duplicate_msgid` is not exercised live (WeCom polls, doesn't re-send
  text) — covered by unit tests. See "Verification: WeCom retry dedup". PASS.
- **Unit tests:** `contrib/agentseek-wecom/tests/test_channel.py` — in-flight
  duplicate reuses the stream; after TTL expiry reprocessing is allowed. PASS.
- **FlClash/DM diagnosis:** tcpdump showed 0 packets for Java's DM SYN under
  TUN; full handshake with FlClash quit. DBeaver (system proxy) connected, JDBC
  (raw socket) didn't. Root cause = FlClash TUN interception. Resolved
  (TUN off / `/32` route). PASS.
- **FlClash/WeCom-API diagnosis:** `openuserid_to_userid` returns `60020` via
  the proxy egress (`45.207.34.86`), returns `zhuchunlin` via direct
  (`112.95.215.20`, allowlisted). Resolved (TUN off). PASS.
- **JVM + ONNX crash — FIXED via subprocess isolation (commit 6eb8f4f).**
  Previously SIGBUS (exit 138) when both `libjvm` and `onnxruntime` were
  in-process. The DM JDBC lookup now runs in a child process
  (`AGENTSEEK_IDENTITY_DM_EXECUTION_MODE=subprocess` → `dm_staff_sidecar`), so
  the gateway process loads only `onnxruntime`, never `libjvm`.
  **Verified live on the Mac mini (2026-06-28), FlClash TUN off:**
  1. `probe_staff_identity.py --oa zhuchunlin` (subprocess mode) → full
     EmployeeContext (朱春霖 / 数智产品研发团队). PASS.
  2. Gateway `我是谁` → `你好，朱春霖！` (identity via subprocess + seekdb ONNX
     loaded in-process). No SIGBUS. PASS.
  3. `请长期记住：我偏好简洁的企微回复，最多三条要点。` → stored to seekdb
     (`已长期记住：…`). No crash. PASS.
  4. `我的长期偏好是什么？` → answered correctly. PASS.
  5. **Restart gateway (clear session) → `我的长期偏好是什么？` again → still
     answered correctly** → seekdb persistence confirmed (not session memory).
     PASS.
  Zero SIGBUS / exit 138 across all steps. `STORAGE_BACKEND=seekdb` is now the
  production setting; `memory` is only a rollback if a crash reappears.

## The DM connection root cause + fix (the big one)

**Symptom:** the DM JDBC bridge (`jaydebeapi` + JPype + `DmJdbcDriver`) could
NOT connect to the DM (`192.10.50.26:5236`) — `网络通信异常` /
`SocketTimeoutException: Read timed out`. DBeaver (same driver jar, same URL,
same creds, same Java 21, same direct path) connected fine and queried data.

**Root cause (confirmed by tcpdump):** FlClash in TUN mode intercepts Java's
raw TCP to the DM. With FlClash running, `tcpdump` showed **0 packets** for
the Java SYN (it never hit the wire); with FlClash quit, the same Java
connected in ~180 ms with a full clean handshake. DBeaver worked because it
uses the macOS *system proxy* (which FlClash forwards correctly); the JDBC
bridge uses raw sockets that don't use the system proxy but get swallowed by
FlClash's TUN capture.

This was mis-diagnosed for a long time. **Ruled out (do not re-investigate):**
driver version (8.1.2.192 and 8.1.3.62 both behave identically), Java version,
schema/database/SSL/compatibleMode URL params, `--add-opens`, IPv4/IPv6 stack,
SOCKS proxy, concurrent-session limit, MAC randomization, the Bash sandbox.
The cause was FlClash, full stop.

**Fix — keep FlClash TUN OFF (system-proxy mode).** This is the practical
resolution on this machine. TUN mode intercepts *all* of the gateway's traffic,
and two destinations cannot go through FlClash's proxy egress:

1. **The DM (`192.10.50.26:5236`)** — FlClash swallows Java's raw TCP to it
   (tcpdump: 0 packets). Fixed by TUN-off, or — if TUN must be on — a `/32`
   static host route that overrides FlClash's split routes by longest-prefix
   match: `sudo route add -host 192.10.50.26 172.20.199.254` (persisted via the
   launchd daemon below).
2. **The WeCom self-built-app API (`qyapi.weixin.qq.com`, `openuserid_to_userid`)**
   — the app has an IP allowlist. Through FlClash's proxy the egress is the
   proxy node's IP (e.g. `45.207.34.86`), which is NOT allowlisted →
   `errcode=60020 "not allow to access from your ip"` → `open_userid` can't be
   resolved → identity fails. This is a *domain* (FlClash fake-ip), so a `/32`
   route does NOT bypass it — **only TUN-off does** (direct egress = the Mac
   mini's real public IP, which is allowlisted; verified returning `zhuchunlin`).

With FlClash **TUN off (system-proxy mode)**, the gateway's traffic (DM raw TCP,
WeCom API via urllib, the model) all go **direct** — they don't use the macOS
system proxy — so DM + WeCom API + model all work; FlClash still proxies
system-proxy-aware apps (browser, etc.). Verified end-to-end:
`我是谁 → 你好，朱春霖！` (full employee context) with FlClash running in
system-proxy mode. **Recommendation: on this machine, never enable FlClash TUN.**

If TUN absolutely must stay on (some app needs it): the DM is handled by the
`/32` route below, but the WeCom API additionally needs an OS-level bypass —
pin `qyapi.weixin.qq.com` to a real IP in `/etc/hosts` and add a `/32` route
for it (same mechanism as the DM, to defeat FlClash's fake-ip). A FlClash
`DIRECT` *rule* does not work (tested — it failed to merge into the active
config, and even merged, TUN still captures the packet first).

`/32` DM route + launchd persistence (only needed if TUN is on):
```bash
sudo route add -host 192.10.50.26 172.20.199.254
sudo cp examples/enterprise_wecom_digital_employee/launchd/com.local.dm-direct-route.plist /Library/LaunchDaemons/
sudo chown root:wheel /Library/LaunchDaemons/com.local.dm-direct-route.plist
sudo launchctl load -w /Library/LaunchDaemons/com.local.dm-direct-route.plist
```

> Note for other machines: the FlClash interception is Mac-mini-specific (the
> dev Mac Pro, wired/without FlClash, never had this). On any machine running
> FlClash/clash TUN, prefer TUN-off for the gateway; if TUN is required, add
> `192.10.50.0/24` to TUN `route-exclude-address` AND pin the WeCom API domain
> direct (a `DIRECT` rule is NOT enough).

## Working configuration (this example's `.env`)

- `AGENTSEEK_IDENTITY_DM_DRIVER_MODULE=agentseek_enterprise.identity.jdbc_driver`
- `AGENTSEEK_IDENTITY_DM_JDBC_JAR=vendor/dameng/DmJdbcDriver18-8.1.3.62.jar`
  (8.1.2.192 also works once FlClash isn't intercepting)
- `AGENTSEEK_IDENTITY_DM_JDBC_JAVA_HOME=/opt/homebrew/opt/openjdk@11/libexec/openjdk.jdk/Contents/Home`
  — **must be Java 11.** JPype 1.7.1 + Java 21 throws
  `ExceptionInInitializerError` on `java.sql.Types`.
- `AGENTSEEK_IDENTITY_DM_EXECUTION_MODE=subprocess` — run the JDBC lookup in a
  child process so the gateway process does not load `libjvm`.
- `AGENTSEEK_IDENTITY_DM_SUBPROCESS_TIMEOUT_SECONDS=30`.
- `AGENTSEEK_ENTERPRISE_IDENTITY_CACHE_ENABLED=true` — cache successful
  employee identity lookups in the gateway process.
- `AGENTSEEK_ENTERPRISE_IDENTITY_CACHE_TTL_SECONDS=600`.
- `AGENTSEEK_ENTERPRISE_IDENTITY_CACHE_MAX_ENTRIES=1024`.
- `AGENTSEEK_CTX_STORAGE_BACKEND=seekdb` — target configuration after
  subprocess isolation. Use `memory` only as a temporary rollback if the Mac
  mini still shows a JVM/ONNX crash.
- `AGENTSEEK_IDENTITY_DM_HOST=192.10.50.26`, port `5236`, user `dbo`.
- Root `.env` contains ONLY `AGENTSEEK_ENV_FILE=examples/enterprise_wecom_digital_employee/.env`.

`dmPython` is unavailable on macOS arm64 (no wheel, no native DM client lib),
so the JDBC bridge (jaydebeapi + JPype1 + the DmJdbcDriver jar + a JDK) is the
only way to reach the DM from a Mac.

## Launch (from repo root, FlClash may be on if the route is active)

```bash
mkdir -p runtime   # one-time; the ./runtime/*.db paths need it
PYTHONPATH="$PWD/examples/enterprise_wecom_digital_employee/src" \
uv run --offline --with jaydebeapi --with JPype1 agentseek gateway \
  --enable-channel wecom --enable-channel mcp.lifecycle --enable-channel skills.lifecycle
```

`--offline` is a safe fallback (deps are cached). With FlClash in system-proxy
mode (the recommended TUN-off setup), uv can also fetch normally through the
live proxy at `127.0.0.1:7890`; `--offline` is only required when FlClash is
fully quit (the dead system proxy then breaks uv's fetch).

Probe one identity without the gateway:
```bash
uv run --env-file examples/enterprise_wecom_digital_employee/.env \
  --with jaydebeapi --with JPype1 \
  python scripts/probe_staff_identity.py --oa <account> --source python-db
```

## Known issues / workarounds

1. **JVM + ONNX SIGBUS crash.** `jaydebeapi`→JPype→`libjvm` (DM JDBC) AND
   ContextSeek's `onnxruntime` (seekdb embedding) in the **same Python
   process** crash with SIGBUS (exit 138) on every message that touches both.
   Confirmed via macOS DiagnosticReports (both `libjvm.dylib` and
   `onnxruntime_pybind11_state.so` loaded). The fix is implemented as
   subprocess identity mode: the child process loads JPype/JDBC; the gateway
   process can load SeekDB/ONNX. Roll back to `STORAGE_BACKEND=memory` only if
   live testing still shows a crash.
2. **JPype + Java 21 incompatible** → use Java 11 (above).
3. **WeCom retry churn.** Fixed in `agentseek-wecom`: text/voice retries with
   the same WeCom `msgid` reuse the original stream response instead of
   launching duplicate agent turns.
4. **`uv` + FlClash system-proxy residue.** Killing FlClash leaves the macOS
   system HTTP/SOCKS proxy pointing at a dead `127.0.0.1:7890`; uv (Rust/reqwest)
   honors it and fails to fetch. `curl` does NOT honor it (so direct endpoints
   still work). Keep FlClash up, or use `--offline`, or clear the system proxy.

## Verification: WeCom retry dedup (2026-06-27, Mac mini)

**Goal:** confirm the retry-churn fix works live, and characterize when
`wecom.duplicate_msgid` does/doesn't fire.

**Method:** added a diagnostic log in `_handle_plain_message`
(`wecom.incoming msgtype={} msgid={}`) to see the msgtype of every decrypted
POST. Restarted the gateway, sent a slow-reply prompt (long-output request →
generation >5 s, triggering WeCom retries).

**Result — msgtype distribution for one slow-reply message:**
- `1 × msgtype=text` (the original message; only one)
- `14 × msgtype=stream` (WeCom polling the stream for the reply; each poll
  carries its own msgid)
- agent turns = 1, replies = 1, `wecom.duplicate_msgid` = 0

**Conclusion:**
- The flood is gone: 15 POSTs → 1 agent turn → 1 reply (vs ~10 replies before
  the fix). WeCom now **polls** (`msgtype=stream` → `_handle_stream_poll`,
  returns the in-progress stream content without a new turn) instead of
  re-sending the message, because the gateway now returns a proper stream
  response (`_stream_response`). This is the primary mechanism that fixed the
  churn.
- `wecom.duplicate_msgid` (the msgid→stream dedup in
  `_get_or_create_stream_for_message`) fires only on a duplicate `msgtype=text`
  POST with the same msgid **while the stream is alive** (TTL
  `cache_ttl_seconds`, default 3600 s). Live, WeCom sends no duplicate text (it
  polls), so this path isn't exercised in normal operation — it's a safety net
  for the text-re-send edge case, covered by the two unit tests in
  `contrib/agentseek-wecom/tests/test_channel.py` (in-flight duplicate reuses
  the stream; after TTL expiry, reprocessing is allowed).
- The `wecom.incoming msgtype=...` diagnostic is intentionally left in place at
  debug level for future WeCom-behavior investigations.

## Next steps / TODO (priority order)

### 1. Verify short-TTL identity cache in the live gateway
Pull the identity-cache commit, set:

```env
AGENTSEEK_IDENTITY_DM_EXECUTION_MODE=subprocess
AGENTSEEK_CTX_STORAGE_BACKEND=seekdb
AGENTSEEK_ENTERPRISE_IDENTITY_CACHE_ENABLED=true
AGENTSEEK_ENTERPRISE_IDENTITY_CACHE_TTL_SECONDS=600
AGENTSEEK_ENTERPRISE_IDENTITY_CACHE_MAX_ENTRIES=1024
```

Then restart the gateway and verify:

- First `我是谁` still resolves the employee identity.
- A second `我是谁` within 10 minutes resolves without a second DM child-process
  lookup. Enable debug logging if you need to see
  `Employee identity cache hit...` explicitly.
- Missing users and lookup errors are **not** cached, so temporary DM failures
  can recover on the next request.

### 2. Long-lived DM sidecar / connection pooling
The short-TTL cache removes repeated DM lookups for active users, but every
cache miss still starts a short-lived child process and opens a new DM
connection. For higher throughput, promote the subprocess bridge into a
long-lived local sidecar with connection pooling and a small request protocol.

### 3. Production hardening
- Run the gateway under a process supervisor (launchd / systemd-equivalent), not
  just the route under launchd.
- Confirm `LANGSMITH_TRACING=true` is intended for prod (or gate it by env).
- `AGENTSEEK_ENTERPRISE_NAMESPACE_SECRET` is set — keep it secret, rotate for prod.

### 4. `agentseek create` for a clean standalone project
Once the template (and the JVM-isolation fix) is stable, generate a clean
standalone project via `agentseek create` for formal deployment/handoff, rather
than running out of the monorepo example.

## Files added/changed in this deployment session (for the Mac Pro pull)

- `examples/enterprise_wecom_digital_employee/DEPLOYMENT_NOTES.md` — this file.
- `examples/enterprise_wecom_digital_employee/launchd/com.local.dm-direct-route.plist`
  — the route-persistence daemon template.
- `vendor/dameng/DmJdbcDriver18-8.1.3.62.jar` — newer DM JDBC driver (optional;
  8.1.2.192 also works once FlClash isn't intercepting).
- `agentseek-enterprise` now supports
  `AGENTSEEK_IDENTITY_DM_EXECUTION_MODE=subprocess` via
  `agentseek_enterprise.identity.dm_staff_sidecar`, so the gateway process can
  keep JPype/libjvm out of the main ContextSeek/ONNX process.
- `agentseek-enterprise` now supports
  `AGENTSEEK_ENTERPRISE_IDENTITY_CACHE_*` for short-TTL successful
  `EmployeeContext` caching in the gateway process.
- `agentseek-wecom` keeps `wecom.incoming msgtype=...` as a debug-level
  diagnostic, reducing normal gateway log noise.
- The `.env` files are gitignored (secrets); copy the working `.env` to the Mac
  Pro manually if you want the same config.
