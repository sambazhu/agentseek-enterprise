"""Explicit TLS-only business transport; no listener starts on import.

Server creation/configuration is an operator action. This module does not open
firewalls, copy credentials, mint approvals or retry uncertain requests.
"""

import base64
from dataclasses import asdict
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import os
import ssl
import threading
import time
from urllib.parse import urlsplit

from .business_broker import WIRE_LIMIT
from .business_execution import BusinessOutcome
from .csv_business import MAX_RESULT_BYTES


def decode_wire(raw):
    if type(raw) is not bytes or len(raw) > WIRE_LIMIT:
        raise ValueError("invalid envelope size")
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result
    value = json.loads(raw, object_pairs_hook=unique)
    if type(value) is not dict:
        raise ValueError("object required")
    return value


class BusinessRequestNotSent(Exception):
    """Connection setup failed before any HTTP request bytes were sent."""


def safe_error(exc):
    """Finite exception type vocabulary; never serialize messages or trace info."""
    allowed = {"ConnectError", "ConnectTimeout", "PoolTimeout", "ReadTimeout", "WriteTimeout",
               "ReadError", "WriteError", "RemoteProtocolError", "LocalProtocolError",
               "SSLError", "SSLCertVerificationError", "TimeoutError", "CancelledError",
               "ValueError", "TypeError", "RuntimeError", "OSError", "PermissionError",
               "BusinessRequestNotSent"}
    name = type(exc).__name__
    return name if name in allowed else "other"


class ExchangeDiagnostic:
    """Per-call scalar trace, independent of HTTP debug logs and request bodies."""

    def __init__(self, request, operation):
        self.started = time.monotonic()
        self.operation = operation
        self.exchange_id = os.urandom(8).hex()
        self.attempt = (hashlib.sha256(json.dumps([request.owner_id, request.request_id]).encode()).hexdigest()
                        if request is not None else "unbound")
        self.stage, self.event, self.failed_stage = "prepare", "started", None
        self.http_status = 0

    def emit(self, status, exc=None):
        value = dict(exchange_id=self.exchange_id, attempt=self.attempt, operation=self.operation,
            stage=self.failed_stage or self.stage, event=self.event, status=status,
            error="none" if exc is None else safe_error(exc),
            cause_error="none" if exc is None or exc.__cause__ is None else safe_error(exc.__cause__),
            elapsed_ms=max(0, int((time.monotonic()-self.started)*1000)),
            http_status=self.http_status, pid=os.getpid(), thread_id=threading.get_native_id())
        logging.getLogger(__name__).warning("business_exchange %s", json.dumps(value, sort_keys=True))

    def trace(self, name, info):
        # info contains headers, exception text and SSL/socket objects. Ignore it.
        phases = {"connection.connect_tcp": "tcp", "connection.start_tls": "tls",
                  "http11.send_request_headers": "send_headers", "http11.send_request_body": "send_body",
                  "http11.receive_response_headers": "response_headers",
                  "http11.receive_response_body": "response_body"}
        phase, _, event = name.rpartition(".")
        if phase in phases and event in {"started", "complete", "failed"}:
            self.stage, self.event = phases[phase], event
            if event == "failed" and self.failed_stage is None:
                self.failed_stage = self.stage
            self.emit("trace")


class BusinessHttpClient:
    def __init__(self, *, endpoint, token, ca_file):
        parsed = urlsplit(endpoint)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
                or parsed.path not in {"", "/"} or parsed.query or parsed.fragment):
            raise ValueError("dedicated HTTPS origin required")
        if not isinstance(token, str) or not 32 <= len(token) <= 256 or any(ord(c) < 33 or ord(c) > 126 for c in token):
            raise ValueError("invalid credential")
        self.endpoint, self._token = endpoint.rstrip("/"), token
        self._tls = ssl.create_default_context(cafile=str(ca_file))

    def exchange(self, request, *, data=None):
        import httpx
        diagnostic = ExchangeDiagnostic(request, "result" if data is None else "execute")
        diagnostic.emit("started")
        try:
            result = self._exchange(request, data=data, diagnostic=diagnostic)
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout) as exc:
            diagnostic.emit("not_sent", exc)
            raise BusinessRequestNotSent("connection establishment failed") from None
        except BaseException as exc:
            diagnostic.emit("unknown", exc)
            raise  # Observability must not change cancellation or uncertainty semantics.
        diagnostic.stage, diagnostic.event = "complete", "complete"
        diagnostic.emit("succeeded")
        return result

    def _exchange(self, request, *, data=None, diagnostic):
        import httpx
        payload = dict(operation="result" if data is None else "execute", request=asdict(request),
                       input="" if data is None else base64.b64encode(data).decode())
        encoded = json.dumps(payload).encode()
        if len(encoded) > WIRE_LIMIT:
            raise ValueError("request too large")
        deadline = time.monotonic() + 70
        diagnostic.stage = "client"
        with httpx.Client(verify=self._tls, timeout=httpx.Timeout(65, connect=5, write=5, pool=5),
                          trust_env=False, follow_redirects=False) as client:
            diagnostic.stage = "pool"
            with client.stream("POST", self.endpoint + "/v1/csv", content=encoded,
                               extensions={"trace": diagnostic.trace},
                               headers={"Authorization": "Bearer " + self._token,
                                        "Content-Type": "application/json", "Accept-Encoding": "identity"}) as response:
                diagnostic.stage = "response_validation"
                diagnostic.http_status = response.status_code
                if (response.status_code != 200 or response.headers.get("Content-Encoding", "identity") != "identity"
                        or response.headers.get("Content-Type", "").split(";")[0] != "application/json"):
                    raise ValueError("broker request rejected")
                raw = bytearray()
                for chunk in response.iter_raw(chunk_size=4096):
                    if time.monotonic() > deadline or len(raw) + len(chunk) > WIRE_LIMIT:
                        raise ValueError("broker response limit")
                    raw.extend(chunk)
        diagnostic.stage = "decode"
        return decode_wire(bytes(raw))


def validated_result(request, response):
    if set(response) != {"outcome", "request_id", "owner_id", "data", "sha256", "workspace"}:
        raise ValueError("invalid result")
    if response["request_id"] != request.request_id or response["owner_id"] != request.owner_id:
        raise ValueError("result scope mismatch")
    outcome = BusinessOutcome(**response["outcome"])
    expected = hashlib.sha256(json.dumps([request.owner_id, request.request_id]).encode()).hexdigest()
    if (outcome.attempt != expected or outcome.state not in {"succeeded", "failed", "reconciling"}
            or type(outcome.cleanup_confirmed) is not bool
            or outcome.cleanup_confirmed != (outcome.state in {"succeeded", "failed"})):
        raise ValueError("inconsistent result state")
    data = None
    if outcome.artifact_ref:
        data = base64.b64decode(response["data"], validate=True)
        digest = hashlib.sha256(data).hexdigest()
        ref = "artifact_" + hashlib.sha256((expected + digest).encode()).hexdigest()
        if (not 0 < len(data) <= MAX_RESULT_BYTES or not data.startswith(b"group,total\n")
                or digest != response["sha256"] or ref != outcome.artifact_ref):
            raise ValueError("artifact integrity failure")
        data.decode("utf-8")
    elif response["data"] is not None or response["sha256"] is not None or outcome.state == "succeeded":
        raise ValueError("missing or unexpected artifact")
    if outcome.state == "succeeded":
        if response["workspace"] != dict(file_id=outcome.artifact_ref, filename="summary.csv",
                                         sha256=hashlib.sha256(data).hexdigest(), size_bytes=len(data)):
            raise ValueError("workspace reference mismatch")
    elif response["workspace"] is not None:
        raise ValueError("uncommitted workspace reference")
    return outcome, data


def make_tls_server(address, *, tls_context, broker):
    if not isinstance(tls_context, ssl.SSLContext) or tls_context.protocol != ssl.PROTOCOL_TLS_SERVER:
        raise ValueError("server TLS context required")
    capacity = threading.BoundedSemaphore(8)
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass  # No headers, request bodies, tokens, or exception messages.

        def do_POST(self):
            self.connection.settimeout(10)
            held = capacity.acquire(blocking=False)
            try:
                if not held or self.path != "/v1/csv" or self.headers.get("Transfer-Encoding"):
                    raise ValueError("rejected")
                lengths = self.headers.get_all("Content-Length", [])
                auth = self.headers.get_all("Authorization", [])
                if (len(lengths) != 1 or len(auth) != 1 or not lengths[0].isdigit()
                        or self.headers.get("Content-Type") != "application/json"):
                    raise ValueError("invalid headers")
                size = int(lengths[0])
                if not 0 < size <= WIRE_LIMIT:
                    raise ValueError("invalid size")
                raw = self.rfile.read(size)
                if len(raw) != size:
                    raise ValueError("truncated")
                value = broker.dispatch(auth[0], decode_wire(raw))
                body, code = json.dumps(value).encode(), 200
                if len(body) > WIRE_LIMIT:
                    raise ValueError("response limit")
            except Exception:
                body, code = b'{"state":"unavailable_or_rejected"}', 400
            finally:
                if held:
                    capacity.release()
            try:
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(body)
            except (OSError, ssl.SSLError):
                pass  # Lost response does not repeat the task.

    class Server(ThreadingHTTPServer):
        daemon_threads = True
        def get_request(self):
            connection, address = super().get_request()
            connection.settimeout(5)
            try:
                return tls_context.wrap_socket(connection, server_side=True), address
            except Exception:
                connection.close()
                raise
    return Server(address, Handler)
