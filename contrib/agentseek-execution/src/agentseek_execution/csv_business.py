"""Bounded CSV vertical slice: durable attempts/artifacts and sandbox-only compute.

The provider owns bounded create/destroy and external deadline supervision.
This module neither creates approvals nor offers a host-shell fallback.
"""

from dataclasses import asdict
from contextlib import contextmanager
import base64
import hashlib
import json
import os
from pathlib import Path
import shlex
import sqlite3

from .business_execution import BusinessRequest
from .secure_ownership import private_file

MAX_CSV_BYTES = 32768
MAX_RESULT_BYTES = 65536

# Executed ONLY in the guest by a trusted command adapter. No model-written code.
CSV_PROGRAM = r'''
import base64, csv, io, json, sys
from decimal import Decimal
data = base64.b64decode(sys.argv[1], validate=True)
if len(data) > 32768: raise ValueError("input limit")
reader = csv.DictReader(io.StringIO(data.decode("utf-8-sig")))
if reader.fieldnames != ["group", "amount"]: raise ValueError("expected group,amount")
totals = {}
for index, row in enumerate(reader):
    if index >= 1000 or None in row: raise ValueError("row limit or extra fields")
    key, raw = row["group"], row["amount"]
    if not key or len(key) > 128 or raw is None or len(raw) > 40: raise ValueError("bad row")
    value = Decimal(raw)
    if not value.is_finite() or abs(value) > Decimal("1e12") or value.as_tuple().exponent < -6:
        raise ValueError("invalid amount")
    totals[key] = totals.get(key, Decimal(0)) + value
output = io.StringIO(newline="")
writer = csv.writer(output, lineterminator="\n")
writer.writerow(["group", "total"])
for key in sorted(totals):
    # Prevent spreadsheet formula execution when the CSV is opened by a user.
    label = "'" + key if key.lstrip().startswith(("=", "+", "-", "@")) else key
    writer.writerow([label, format(totals[key], "f")])
result = output.getvalue().encode("utf-8")
if len(result) > 65536: raise ValueError("output limit")
print(json.dumps({"csv": base64.b64encode(result).decode("ascii"), "groups": len(totals)}))
'''.strip()


def csv_command(data: bytes) -> str:
    if type(data) is not bytes or not 0 < len(data) <= MAX_CSV_BYTES:
        raise ValueError("invalid CSV size")
    return "python3 -I -c " + shlex.quote(CSV_PROGRAM) + " " + shlex.quote(base64.b64encode(data).decode("ascii"))


def decode_csv_result(stdout: str) -> bytes:
    if not isinstance(stdout, str) or len(stdout.encode()) > 100000:
        raise ValueError("invalid result size")
    result = json.loads(stdout)
    if set(result) != {"csv", "groups"} or type(result["groups"]) is not int or not 0 <= result["groups"] <= 1000:
        raise ValueError("invalid result")
    data = base64.b64decode(result["csv"], validate=True)
    if not 0 < len(data) <= MAX_RESULT_BYTES or not data.startswith(b"group,total\n"):
        raise ValueError("invalid output")
    data.decode("utf-8")
    return data


class BusinessStore:
    """Private SQLite attempts plus immutable artifact bytes, owner-scoped reads.

    An interrupted reservation stays active and blocks another create. Recovery
    is an explicit operator action; this API never resets an attempt.
    """

    def __init__(self, directory: Path):
        if not directory.is_absolute() or directory.resolve() != directory:
            raise ValueError("canonical private directory required")
        info = directory.stat()
        if info.st_uid != os.getuid() or info.st_mode & 0o777 != 0o700:
            raise ValueError("private directory required")
        self.path = directory / "business.sqlite"
        try:
            fd = private_file(self.path, create=True)
        except FileExistsError:
            fd = private_file(self.path)
        os.close(fd)
        with self._connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS attempts(
                    id TEXT PRIMARY KEY, owner TEXT NOT NULL, request TEXT NOT NULL,
                    state TEXT NOT NULL, artifact TEXT);
                CREATE TABLE IF NOT EXISTS artifacts(
                    id TEXT PRIMARY KEY, owner TEXT NOT NULL, sha TEXT NOT NULL, data BLOB NOT NULL);
            """)

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        try:
            db.execute("PRAGMA synchronous=FULL")
            with db:
                yield db
        finally:
            db.close()

    def reserve(self, request: BusinessRequest) -> str:
        attempt = hashlib.sha256(json.dumps([request.owner_id, request.request_id]).encode()).hexdigest()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM attempts WHERE id=?", (attempt,)).fetchone():
                raise ValueError("attempt already exists; do not retry")
            if db.execute("SELECT 1 FROM attempts WHERE state NOT IN ('succeeded','failed')").fetchone():
                raise ValueError("unresolved attempt; do not create")
            db.execute("INSERT INTO attempts VALUES (?,?,?,'reserved',NULL)",
                       (attempt, request.owner_id, json.dumps(asdict(request), sort_keys=True)))
        return attempt

    def persist(self, attempt: str, owner: str, data: bytes) -> str:
        if type(data) is not bytes or not 0 < len(data) <= MAX_RESULT_BYTES:
            raise ValueError("invalid artifact size")
        digest = hashlib.sha256(data).hexdigest()
        ref = "artifact_" + hashlib.sha256((attempt + digest).encode()).hexdigest()
        with self._connect() as db:
            row = db.execute("SELECT owner,state FROM attempts WHERE id=?", (attempt,)).fetchone()
            if row != (owner, "reserved"):
                raise ValueError("invalid artifact owner or state")
            db.execute("INSERT INTO artifacts VALUES (?,?,?,?)", (ref, owner, digest, data))
            db.execute("UPDATE attempts SET artifact=? WHERE id=?", (ref, attempt))
        if self.read(owner, ref) != data:
            raise ValueError("artifact readback failed")
        return ref

    def read(self, owner: str, ref: str) -> bytes:
        with self._connect() as db:
            row = db.execute("SELECT sha,data FROM artifacts WHERE id=? AND owner=?", (ref, owner)).fetchone()
        if row is None or hashlib.sha256(row[1]).hexdigest() != row[0]:
            raise ValueError("artifact unavailable")
        return row[1]

    def record(self, attempt: str, state: str, artifact: str | None):
        if state not in {"succeeded", "failed", "reconciling"}:
            raise ValueError("invalid outcome")
        with self._connect() as db:
            row = db.execute("SELECT artifact FROM attempts WHERE id=?", (attempt,)).fetchone()
            if row is None or (artifact is not None and row[0] != artifact):
                raise ValueError("outcome mismatch")
            db.execute("UPDATE attempts SET state=? WHERE id=?", (state, attempt))


class CsvBusinessBackend:
    """One backend per request. Provider is installed server code, never model data.

    provider.create(attempt) must persist identity and register supervision before
    returning. provider.run(attempt, command, timeout) executes only in that guest.
    provider.destroy(attempt) verifies absence, including uncertain-create cases.
    """

    def __init__(self, *, request, store, provider, authorize, load_input):
        self.request, self.store, self.provider = request, store, provider
        self._authorize, self._load_input = authorize, load_input
        self._data = None
        self._result = None

    def authorize(self, request):
        if request != self.request or self._authorize(request) is not True:
            raise ValueError("request denied")
        # Input access/size failures must happen before consuming a sandbox.
        self._data = self._load_input(request)
        csv_command(self._data)
        self.provider.validate_request(request, self._data)

    def reserve(self, request):
        return self.store.reserve(request)

    def create(self, attempt):
        self.provider.create(attempt)

    def prepare(self, attempt, input_ref):
        if input_ref != self.request.input_ref or self._authorize(self.request) is not True:
            raise ValueError("input no longer authorized")
        # Bounded input is transferred as an encoded argument, not a host mount.

    def execute(self, attempt, instruction):
        if instruction != self.request.instruction:
            raise ValueError("instruction changed")
        output = self.provider.run(attempt, csv_command(self._data), timeout=15)
        self._result = decode_csv_result(output)
        return "result"

    def persist(self, attempt, result_ref):
        if result_ref != "result" or self._authorize(self.request) is not True:
            raise ValueError("output no longer authorized")
        return self.store.persist(attempt, self.request.owner_id, self._result)

    def destroy(self, attempt):
        return self.provider.destroy(attempt)

    def record(self, attempt, state, artifact):
        self.store.record(attempt, state, artifact)
