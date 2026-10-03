"""Real supervisor subprocess/HTTP/registration, synthetic loopback platform only.

Targets have already-expired synthetic timestamps. This verifies process wiring,
not a real 120-second guest lifetime or Linux service installation.
"""

import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest


def wait_for(predicate, process, timeout=8):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        assert process.poll() is None, "supervisor exited unexpectedly"
        if predicate():
            return
        time.sleep(0.025)
    pytest.fail("supervisor condition timed out")


@pytest.mark.parametrize("slots", [2, 4])
def test_independent_supervisor_reloads_registration_and_preserves_history(tmp_path, slots):
    source = (Path(__file__).resolve().parents[3] / "examples" /
              "enterprise_wecom_digital_employee/sandbox_poc/node_supervisor.py")
    manifest = tmp_path / "manifest.json"
    alarm = tmp_path / "alarm"
    heartbeat = tmp_path / "manifest.json.heartbeat"
    command = [sys.executable, "-I", str(source), "--manifest", str(manifest),
               "--alarm-file", str(alarm)]
    env = {k: v for k, v in os.environ.items() if k not in {"PYTHONPATH", "PYTHONHOME"}}

    def register(*args):
        return subprocess.run([*command, *args], env=env, cwd=tmp_path, umask=0o077,  # noqa: S603 -- fixed local test CLI
                              capture_output=True, timeout=5, check=True)

    register("--register-run", "synthetic-r1", "synthetic-alias", "synthetic-template")
    records = {}
    deletes = []
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            with lock:
                value = list(records.values()) if self.path == "/sandboxes" else records.get(self.path.rsplit("/", 1)[-1])
                body = json.dumps(value).encode()
            self.send_response(200 if value is not None else 404)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_DELETE(self):
            with lock:
                deletes.append(self.path.rsplit("/", 1)[-1])
            # Keep the target visible until the test acknowledges disappearance.
            self.send_response(204)
            self.end_headers()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    process = None
    try:
        with (tmp_path / "supervisor.log").open("wb") as log:
            process = subprocess.Popen(  # noqa: S603 -- fixed source, synthetic loopback endpoint
                                       [*command, "--api-url", f"http://127.0.0.1:{server.server_port}"],
                                       env=env, cwd=tmp_path, umask=0o077,
                                       stdout=log, stderr=log)
            wait_for(heartbeat.exists, process)
            assert json.loads(heartbeat.read_bytes())["pid"] == process.pid
            assert process.pid != os.getpid()
            for slot in range(slots):
                sid = f"synthetic-{slot}"
                register("--register-sandbox", sid)
                before = manifest.read_bytes()
                register("--register-sandbox", sid)
                assert manifest.read_bytes() == before, "registration replay changed history"
                assert set(json.loads(before)["sandboxes"]) == {f"synthetic-{i}" for i in range(slot + 1)}
                with lock:
                    records[sid] = {"sandboxID": sid, "templateID": "synthetic-template",
                                    "state": "running", "startedAt": time.time() - 121,
                                    "metadata": {}}
                wait_for(lambda sid=sid: sid in deletes and alarm.exists(), process)
                assert "termination pending" in alarm.read_text()
                with lock:
                    records.clear()
                wait_for(lambda: not alarm.exists(), process)
                assert manifest.read_bytes() == before
                assert json.loads(heartbeat.read_bytes())["pid"] == process.pid
                assert heartbeat.stat().st_mode & 0o777 == 0o600
            assert deletes == [f"synthetic-{i}" for i in range(slots)]
            assert manifest.stat().st_mode & 0o777 == 0o600
    finally:
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
