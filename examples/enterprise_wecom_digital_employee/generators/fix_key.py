import hashlib, os
from pathlib import Path
DST = Path("/var/lib/agentseek-m3/runs/agentseek-m3-r1-20260917/config/api_key")
raw = DST.read_bytes()
assert raw.endswith(b"\n") and raw.count(b"\n") == 1 and b"=" not in raw, "unexpected own-file content"
fixed = raw.rstrip(b"\n")
fd = os.open(DST, os.O_WRONLY | os.O_TRUNC | os.O_NOFOLLOW)
try:
    os.write(fd, fixed); os.fsync(fd)
finally:
    os.close(fd)
print("fixed: len:", len(fixed), "sha256:", hashlib.sha256(fixed).hexdigest(), "mode:", oct(os.stat(DST).st_mode & 0o777))
