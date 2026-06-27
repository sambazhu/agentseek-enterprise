# Enterprise WeCom Digital Employee — Deployment Notes (Mac mini)

Handoff notes from deploying/verifying this example on a company Mac mini
(branch `enterprise/wecom-runtime`, 2026-06-27). Covers the DM-connection
root cause, the working configuration, known-issue workarounds, and the
remaining work items.

## Verified working (end-to-end)

WeCom callback → signature verify/decrypt → `open_userid` → plaintext userid
(e.g. `zhuchunlin`) → DM identity lookup (`朱春霖 / 数智产品研发团队 / 团队长兼数据架构师`)
→ model (`glm-5.2` via DashScope) → reply. Confirmed with `我是谁 → 你好，朱春霖！...`
including full org path/role. FlClash running (TUN) the whole time.

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

**Fix (confirmed in production):** add a `/32` static host route that overrides
FlClash's TUN split routes (`0/1` + `128/1`) by longest-prefix match, forcing
DM traffic direct via `en1` while FlClash keeps proxying everything else:

```bash
sudo route add -host 192.10.50.26 172.20.199.254
```

Persist across reboots with a launchd LaunchDaemon
(`RunAtLoad` + `StartInterval 60`) at
`/Library/LaunchDaemons/com.local.dm-direct-route.plist` running that `route add`.
Template is committed at `examples/enterprise_wecom_digital_employee/launchd/com.local.dm-direct-route.plist`
— install with:
```bash
sudo cp examples/enterprise_wecom_digital_employee/launchd/com.local.dm-direct-route.plist /Library/LaunchDaemons/
sudo chown root:wheel /Library/LaunchDaemons/com.local.dm-direct-route.plist
sudo launchctl load -w /Library/LaunchDaemons/com.local.dm-direct-route.plist
```

> Note for other machines: the FlClash interception is Mac-mini-specific (the
> dev Mac Pro, wired/without FlClash, never had this). On any machine running
> FlClash/clash TUN, either add the static host route or put `192.10.50.0/24`
> in TUN `route-exclude-address` (a `DIRECT` *rule* is NOT enough — TUN still
> captures the packet first).

## Working configuration (this example's `.env`)

- `AGENTSEEK_IDENTITY_DM_DRIVER_MODULE=agentseek_enterprise.identity.jdbc_driver`
- `AGENTSEEK_IDENTITY_DM_JDBC_JAR=vendor/dameng/DmJdbcDriver18-8.1.3.62.jar`
  (8.1.2.192 also works once FlClash isn't intercepting)
- `AGENTSEEK_IDENTITY_DM_JDBC_JAVA_HOME=/opt/homebrew/opt/openjdk@11/libexec/openjdk.jdk/Contents/Home`
  — **must be Java 11.** JPype 1.7.1 + Java 21 throws
  `ExceptionInInitializerError` on `java.sql.Types`.
- `AGENTSEEK_CTX_STORAGE_BACKEND=memory` — **temporary**, to avoid the
  JVM+ONNX crash (see below). Revert to `seekdb` once JVM isolation is done.
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

`--offline` because with FlClash off, the macOS system proxy it leaves behind
(dead `127.0.0.1:7890`) makes uv's package fetch fail; the deps are cached.
With FlClash TUN on (+ route), plain `uv run --with ...` also works.

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
   `onnxruntime_pybind11_state.so` loaded). Workaround: `STORAGE_BACKEND=memory`
   (keyword-only, no ONNX). Proper fix = JVM subprocess isolation (see below).
2. **JPype + Java 21 incompatible** → use Java 11 (above).
3. **WeCom retry churn.** Fixed in `agentseek-wecom`: text/voice retries with
   the same WeCom `msgid` reuse the original stream response instead of
   launching duplicate agent turns.
4. **`uv` + FlClash system-proxy residue.** Killing FlClash leaves the macOS
   system HTTP/SOCKS proxy pointing at a dead `127.0.0.1:7890`; uv (Rust/reqwest)
   honors it and fails to fetch. `curl` does NOT honor it (so direct endpoints
   still work). Keep FlClash up, or use `--offline`, or clear the system proxy.

## Next steps / TODO (priority order)

### 1. JVM subprocess isolation — restore `seekdb` semantic memory (highest value)
Currently `STORAGE_BACKEND=memory` to avoid the JVM+ONNX crash. Goal: run the
DM JDBC access in a **separate subprocess** (a small Java sidecar, or a Python
child using jaydebeapi) so the main gateway process never loads `libjvm` →
ONNX/seekdb can coexist with identity. Then revert `STORAGE_BACKEND` to `seekdb`.
Touch points:
- `contrib/agentseek-enterprise/src/agentseek_enterprise/identity/dm_staff_provider.py`
  and `jdbc_driver.py` — replace the in-process `jaydebeapi.connect` with an RPC
  to the sidecar (stdin/JSON or a local HTTP socket).
- Ship a tiny sidecar entrypoint (Java `main` or a Python script) that takes an
  OA account and returns the employee-context JSON.
- Verify: gateway with `seekdb` + identity, no SIGBUS, `我是谁` still resolves.

### 2. DM connection robustness
Each identity lookup currently opens a new DM connection (slow; re-triggers the
JVM/JDBC path every message). Consider pooling, or caching `employee_context`
per `(tenant, user)` for a short TTL once JPype warmup cost is isolated.

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
- No Python/source code was modified. The `.env` files are gitignored (secrets);
  copy the working `.env` to the Mac Pro manually if you want the same config.
