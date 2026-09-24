"""Private, bounded CSV command/termination worker for an approved same-host pilot.

Not a public broker or a replacement for lifecycle admission. No create endpoint,
shell fallback, SDK implicit retry or arbitrary model command is exposed.
"""

import base64
from dataclasses import asdict, replace
import hashlib
import json
import logging
import math
import os
import re
import shlex
import struct
import sys
import time

from .csv_business import CSV_PROGRAM, MAX_CSV_BYTES, decode_csv_result
from .m3_create_process import _path, _pinned
from .m3_create_receipt import CreateBinding, CreateReceiptVault
from .m3_platform_evidence import PlatformReader
from .m3_probe_process import _decode, config_bytes
from .m3_probe_protocol import _CommandState
from .m3_supervisor_identity import IdentityPins, verify_identity
from .m3_supervisor_snapshot import SupervisorReader, system_clock
from .models import Code, canonical, require
from .worker_process import run_worker

LIMIT = 200000
READY_BUDGET = 5
COMMAND_BUDGET = 15


def wait_ready(client, origin, headers, deadline, diagnostic):
    """Only a read-only health request may be retried; never Process.Start."""
    import httpx
    diagnostic["stage"] = "readiness"
    for attempt in range(1, 7):
        remaining = deadline - time.monotonic()
        require(remaining > 0, Code.DENIED)
        diagnostic["ready_attempts"] = attempt
        try:
            with client.stream("GET", origin + "/health", headers=headers,
                               timeout=httpx.Timeout(min(1.0, remaining))) as response:
                diagnostic["http_status"] = response.status_code
                if response.status_code in {200, 204}:
                    require(time.monotonic() < deadline, Code.DENIED)
                    return
                require(response.status_code in {502, 503, 504}, Code.DENIED)
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout):
            pass
        if attempt < 6:
            time.sleep(min(0.2, max(0, deadline - time.monotonic())))
    raise RuntimeError("readiness exhausted")


def read_plan(path, digest):
    value = _decode(_pinned(path, digest))
    fields = {"schema", "csv_approved", "termination_approved", "expires_epoch",
        "expected_create", "input_sha256", "instruction_sha256", "vault_directory", "vault_key_file",
        "control", "supervisor_directory", "supervisor_identity"}
    require(set(value) in (fields, fields | {"diagnostics_enabled"})
            and type(value.get("diagnostics_enabled", False)) is bool, Code.DENIED)
    require(type(value["schema"]) is int and value["schema"] == 1
            and value["csv_approved"] is True and value["termination_approved"] is True, Code.DENIED)
    expected = CreateBinding(**value["expected_create"])
    expected.validate()
    require(expected.intent_sha256 == "0" * 64 and expected.restricted is True, Code.DENIED)
    for name in ("input_sha256", "instruction_sha256"):
        require(type(value[name]) is str and len(value[name]) == 64
                and all(c in "0123456789abcdef" for c in value[name]), Code.DENIED)
    expiry = value["expires_epoch"]
    require(type(expiry) in (int, float) and math.isfinite(expiry) and expiry > time.time(), Code.DENIED)
    return value


class ApprovedCsvSessionFactory:
    """Bind one immutable business plan before create and its real receipt after."""

    def __init__(self, path, digest):
        self.path, self.digest = _path(str(path)), digest

    def validate_request(self, request, data):
        plan = read_plan(self.path, self.digest)
        require(hashlib.sha256(data).hexdigest() == plan["input_sha256"]
                and hashlib.sha256(request.instruction.encode()).hexdigest() == plan["instruction_sha256"], Code.DENIED)
        return plan["expected_create"]

    def __call__(self, binding):
        plan = read_plan(self.path, self.digest)
        created = CreateBinding(**binding)
        created.validate()
        require(created.intent_sha256 != "0" * 64
                and asdict(replace(created, intent_sha256="0" * 64)) == plan["expected_create"], Code.DENIED)
        return ApprovedCsvSession(self.path, self.digest, binding)

    def validate_installation(self, installed, precreate):
        plan = read_plan(self.path, self.digest)
        require(_path(plan["vault_directory"]) == _path(installed["vault_directory"])
                and _path(plan["vault_key_file"]) == _path(installed["receipt_key_file"]), Code.DENIED)
        for name in ("supervisor_directory", "supervisor_identity"):
            require(plan[name] == precreate[name], Code.DENIED)
        control = plan["control"]
        require(set(control) == {"endpoint", "api_key", "ca_file", "domain", "proxy_port"}
                and control["endpoint"] == precreate["plan"]["endpoint"]
                and control["domain"] == precreate["plan"]["domain"]
                and type(control["proxy_port"]) is int and control["proxy_port"] == 80
                and _path(control["ca_file"]) == _path(precreate["ca_file"])
                and control["api_key"].encode("ascii") == config_bytes(_path(precreate["api_key_file"])), Code.DENIED)


class ApprovedCsvSession:
    def __init__(self, path, digest, binding):
        self.path, self.digest, self.binding = path, digest, binding

    def _invoke(self, operation, data=""):
        payload = dict(path=str(self.path), sha256=self.digest, binding=self.binding, operation=operation, data=data)
        try:
            raw = run_worker([sys.executable, "-I", "-m", "agentseek_execution.business_cube_session"],
                             canonical(payload).encode(), budget=25 if operation == "run" else 10, environment={})
        except Exception:
            logging.getLogger(__name__).warning("csv_worker operation=%s status=worker_unknown", operation)
            raise
        value = _decode(raw)
        if set(value) == {"result", "diagnostic"}:
            diagnostic = value["diagnostic"]
            # Never log child exception messages, headers, paths or payloads.
            allowed_stages = {"validation", "identity", "readiness", "command", "stream", "decode", "terminate", "complete"}
            allowed_errors = {"none", "timeout", "connect", "contract", "protocol", "program", "internal"}
            fields = {"stage", "error", "elapsed_ms", "ready_attempts", "http_status"}
            require(type(diagnostic) is dict and set(diagnostic) in (fields, fields | {"evidence"})
                    and diagnostic["stage"] in allowed_stages and diagnostic["error"] in allowed_errors
                    and all(type(diagnostic[k]) is int and 0 <= diagnostic[k] <= 600000
                            for k in ("elapsed_ms", "ready_attempts", "http_status")), Code.UNKNOWN)
            if "evidence" in diagnostic:
                require(value["result"] is None, Code.UNKNOWN)
                validate_evidence(diagnostic["evidence"])
            logging.getLogger(__name__).warning("csv_worker operation=%s diagnostic=%s", operation, canonical(diagnostic))
            require(value["result"] is not None, Code.UNKNOWN)
            return value["result"]
        return value

    def run(self, command, timeout):
        args = shlex.split(command)
        require(timeout == 15 and len(args) == 5 and args[:3] == ["python3", "-I", "-c"]
                and args[3] == CSV_PROGRAM, Code.DENIED)
        result = self._invoke("run", args[4])
        require(set(result) == {"stdout"}, Code.UNKNOWN)
        decode_csv_result(result["stdout"])
        return result["stdout"]

    def terminate(self):
        require(self._invoke("terminate") == {"delete_accepted": True}, Code.UNKNOWN)

    def close(self):
        pass  # All HTTP clients live in bounded children and are already closed.


class CommandProtocolError(ValueError):
    """Malformed/ambiguous transport envelope; never contains response text."""


class CommandProgramError(ValueError):
    """Explicit process failure or invalid application result; no output text."""


def new_evidence():
    return dict(substage="frames", frames=[], frames_truncated=False, stdout_bytes=0,
                stderr_bytes=0, end_seen=False, stream_end_seen=False, exit_code=None)


def validate_evidence(value):
    require(type(value) is dict and set(value) == set(new_evidence()), Code.UNKNOWN)
    require(value["substage"] in {"frames", "frame_json", "event", "end_event", "process_result", "stdout_utf8", "csv_result"}, Code.UNKNOWN)
    require(type(value["frames"]) is list and len(value["frames"]) <= 32, Code.UNKNOWN)
    for pair in value["frames"]:
        require(type(pair) is list and len(pair) == 2 and all(type(n) is int for n in pair)
                and 0 <= pair[0] <= 255 and 0 <= pair[1] <= 4294967295, Code.UNKNOWN)
    require(all(type(value[k]) is bool for k in ("frames_truncated", "end_seen", "stream_end_seen")), Code.UNKNOWN)
    require(all(type(value[k]) is int and 0 <= value[k] <= LIMIT for k in ("stdout_bytes", "stderr_bytes")), Code.UNKNOWN)
    require(value["exit_code"] is None or type(value["exit_code"]) is int and -2147483648 <= value["exit_code"] <= 2147483647, Code.UNKNOWN)


def end_code(end):
    """SDK 0.7 status vocabulary, with conflicting evidence rejected, no default 0."""
    if type(end) is not dict or end.get("error"):
        raise CommandProgramError if type(end) is dict and end.get("error") else CommandProtocolError
    codes = []
    if "exitCode" in end and "exit_code" in end:
        raise CommandProtocolError
    for name in ("exitCode", "exit_code"):
        if name in end:
            code = end[name]
            if type(code) is not int or not -2147483648 <= code <= 2147483647:
                raise CommandProtocolError
            codes.append(code)
    if "status" in end:
        status = end["status"]
        if type(status) is not str:
            raise CommandProtocolError
        if status == "exited":
            codes.append(0)
        else:
            match = re.fullmatch(r"(?:exit status|exited with code) (-?\d{1,10})", status)
            signal = re.fullmatch(r"(?:signal|terminated by signal) (\d{1,3})", status)
            if match:
                codes.append(int(match[1]))
            elif signal and 1 <= int(signal[1]) <= 127:
                codes.append(128 + int(signal[1]))
            else:
                raise CommandProtocolError
    if not codes or len(set(codes)) != 1 or not -2147483648 <= codes[0] <= 2147483647:
        raise CommandProtocolError
    return codes[0]


def command_stdout(body, evidence=None):
    """Bounded Connect stream; zero exit and complete terminator required."""
    from .models import ContractError
    try:
        return _command_stdout(body, evidence if evidence is not None else new_evidence())
    except (CommandProgramError, CommandProtocolError):
        raise
    except (ContractError, ValueError, UnicodeError, TypeError, KeyError):
        raise CommandProtocolError from None


def _command_stdout(body, evidence):
    require(type(body) is bytes and len(body) <= LIMIT, Code.UNKNOWN)
    state, offset, ended = _CommandState(), 0, False
    while offset < len(body):
        require(not ended and len(body) - offset >= 5, Code.UNKNOWN)
        flag = body[offset]
        size = struct.unpack(">I", body[offset + 1:offset + 5])[0]
        offset += 5
        evidence["substage"] = "frames"
        if len(evidence["frames"]) < 32:
            evidence["frames"].append([flag, size])
        else:
            evidence["frames_truncated"] = True
        require(flag in (0, 2) and size <= LIMIT and offset + size <= len(body), Code.UNKNOWN)
        evidence["substage"] = "frame_json"
        value = {} if flag == 2 and size == 0 else _decode(body[offset:offset + size])
        offset += size
        if flag == 2:
            require(not value.get("error"), Code.UNKNOWN)
            ended = True
            evidence["stream_end_seen"] = True
        else:
            evidence["substage"] = "event"
            event = value.get("event")
            if type(event) is dict and set(event) == {"end"}:
                evidence["substage"] = "end_event"
                value = {"event": {"end": {"exitCode": end_code(event["end"])}}}
            state.event(value)
            evidence.update(stdout_bytes=len(state.stdout), stderr_bytes=len(state.stderr),
                            end_seen=state.ended, exit_code=state.exit_code)
    require(ended and state.ended, Code.UNKNOWN)
    evidence["substage"] = "process_result"
    if state.exit_code != 0 or state.stderr:
        raise CommandProgramError
    evidence["substage"] = "stdout_utf8"
    try:
        return bytes(state.stdout).decode("utf-8")
    except UnicodeError:
        raise CommandProgramError from None


def decode_command(body, evidence=None):
    stdout = command_stdout(body, evidence)
    if evidence is not None:
        evidence["substage"] = "csv_result"
    try:
        decode_csv_result(stdout)
    except (ValueError, UnicodeError, TypeError, KeyError):
        raise CommandProgramError from None
    return stdout


def classify_error(exc):
    import httpx
    from .models import ContractError
    return ("timeout" if isinstance(exc, httpx.TimeoutException) else
            "connect" if isinstance(exc, httpx.ConnectError) else
            "program" if isinstance(exc, CommandProgramError) else
            "protocol" if isinstance(exc, CommandProtocolError) else
            "contract" if isinstance(exc, ContractError) else
            "protocol" if isinstance(exc, (ValueError, UnicodeError)) else "internal")


def perform(payload, diagnostic=None):
    import httpx
    diagnostic = diagnostic if diagnostic is not None else {}
    diagnostic["stage"] = "validation"
    worker_deadline = time.monotonic() + 23  # outer worker remains 25s; reserve IPC/exit
    require(sys.platform == "linux" and os.getuid() == os.geteuid() == 0, Code.DENIED)
    require(set(payload) == {"path", "sha256", "binding", "operation", "data"}, Code.DENIED)
    plan = read_plan(_path(payload["path"]), payload["sha256"])
    binding = CreateBinding(**payload["binding"])
    binding.validate()
    require(binding.intent_sha256 != "0" * 64
            and asdict(replace(binding, intent_sha256="0" * 64)) == plan["expected_create"], Code.DENIED)
    require(binding.boot_id == system_clock().boot_id, Code.DENIED)
    vault = CreateReceiptVault(_path(plan["vault_directory"]), config_bytes(_path(plan["vault_key_file"])))
    intent, receipt = vault.read_intent(binding), vault.read(binding)
    require(receipt.domain == binding.domain and receipt.template_id == binding.template_id, Code.DENIED)
    control = dict(plan["control"])
    control["ca_file"] = _path(control["ca_file"])
    reader = PlatformReader(**control)
    snapshot = reader.collect_created(binding, vault)
    operation = payload["operation"]
    require(operation in {"run", "terminate"}, Code.DENIED)
    if operation == "terminate":
        diagnostic["stage"] = "terminate"
        require(payload["data"] == "", Code.DENIED)
        # Target identity was verified above; absence confirmation remains the
        # independent lifecycle closeout. A lost DELETE response is not success.
        with httpx.Client(verify=reader._tls, timeout=2, trust_env=False, follow_redirects=False) as client:
            response = client.delete(control["endpoint"].rstrip("/") + "/sandboxes/" + receipt.sandbox_id,
                                     headers={"X-API-Key": control["api_key"]})
            require(response.status_code in {200, 204}, Code.UNKNOWN)
        return {"delete_accepted": True}
    diagnostic["stage"] = "identity"
    supervisor = SupervisorReader(_path(plan["supervisor_directory"])).read(
        run_id=binding.run_id, template_id=binding.template_id, sandbox_id=receipt.sandbox_id)
    verify_identity(supervisor, IdentityPins(**plan["supervisor_identity"]))
    now = system_clock()
    require(now.monotonic >= intent["sent_mono"] and now.monotonic - intent["sent_mono"] <= 90
            and snapshot.started_epoch is not None and time.time() + 30 <= snapshot.started_epoch + 120
            and time.time() + 30 <= plan["expires_epoch"], Code.DENIED)
    data = base64.b64decode(payload["data"], validate=True)
    require(0 < len(data) <= MAX_CSV_BYTES and hashlib.sha256(data).hexdigest() == plan["input_sha256"], Code.DENIED)
    require(receipt.traffic_token and receipt.envd_state in {"null", "absent"}, Code.DENIED)
    # v0.7 receipt has no envd credential; this pilot relies on the verified
    # private CubeProxy traffic token. It does not claim envd-token enforcement.
    headers = {"Host": f"49983-{receipt.sandbox_id}.{receipt.domain}", "X-API-Key": control["api_key"],
        "e2b-traffic-access-token": receipt.traffic_token, "Authorization": "Basic cm9vdDo=",
        "Content-Type": "application/connect+json", "Connect-Protocol-Version": "1",
        "Connect-Timeout-Ms": "15000", "Accept-Encoding": "identity", "Connect-Content-Encoding": "identity"}
    value = dict(process=dict(cmd="python3", args=["-I", "-c", CSV_PROGRAM, payload["data"]], envs={}), stdin=False)
    encoded = json.dumps(value).encode()
    body = b"\x00" + struct.pack(">I", len(encoded)) + encoded
    origin = f"http://127.0.0.1:{control['proxy_port']}"
    with httpx.Client(timeout=httpx.Timeout(15, connect=2, write=2, pool=2),
                      trust_env=False, follow_redirects=False) as client:
        require(worker_deadline - time.monotonic() >= READY_BUDGET + COMMAND_BUDGET, Code.DENIED)
        wait_ready(client, origin, headers, min(time.monotonic() + READY_BUDGET,
                                               worker_deadline - COMMAND_BUDGET), diagnostic)
        # Readiness time consumes existing budgets, never extends the guest or approval.
        require(worker_deadline - time.monotonic() >= COMMAND_BUDGET
                and time.time() + 20 <= snapshot.started_epoch + 120
                and time.time() + 20 <= plan["expires_epoch"], Code.DENIED)
        diagnostic["stage"] = "command"
        with client.stream("POST", origin + "/process.Process/Start",
                           headers=headers, content=body) as response:
            diagnostic["http_status"] = response.status_code
            require(response.status_code == 200 and response.headers.get("Content-Encoding", "identity") == "identity"
                    and response.headers.get("Content-Type", "").split(";")[0] == "application/connect+json", Code.UNKNOWN)
            buffer = bytearray()
            diagnostic["stage"] = "stream"
            for part in response.iter_raw(chunk_size=4096):
                require(time.monotonic() < worker_deadline, Code.UNKNOWN)
                require(len(buffer) + len(part) <= LIMIT, Code.UNKNOWN)
                buffer.extend(part)
    diagnostic["stage"] = "decode"
    evidence = new_evidence() if plan.get("diagnostics_enabled", False) else None
    try:
        stdout = decode_command(bytes(buffer), evidence)
    except Exception:
        if evidence is not None:
            diagnostic["evidence"] = evidence
        raise
    return {"stdout": stdout}


def main():
    import httpx
    started = time.monotonic()
    diagnostic = dict(stage="validation", error="none", elapsed_ms=0, ready_attempts=0, http_status=0)
    result = None
    try:
        require(sys.flags.isolated, Code.DENIED)
        raw = sys.stdin.buffer.read(65537)
        require(len(raw) <= 65536, Code.DENIED)
        payload = _decode(raw)
        require(set(payload) == {"path", "sha256", "binding", "operation", "data"}, Code.DENIED)
    except Exception:
        sys.exit(2)
    try:
        result = perform(payload, diagnostic)
        diagnostic["stage"] = "complete"
    except Exception as exc:
        diagnostic["error"] = classify_error(exc)
    diagnostic["elapsed_ms"] = min(600000, max(0, int((time.monotonic() - started) * 1000)))
    print(canonical(dict(result=result, diagnostic=diagnostic)))


if __name__ == "__main__":
    main()
