"""api_key 物化（本轮授权）：受控源 poc-install.env 的 CUBE_API_KEY → 纯 key 字节文件。
进程内读取，不打印；目标已存在即停（不覆盖）；0600/父 0700；仅输出摘要与长度。"""
import hashlib, os, sys
from pathlib import Path
SRC = Path("/root/cube-one-click-v0.7.0/poc-install.env")
DST = Path("/var/lib/agentseek-m3/runs/agentseek-m3-r1-20260917/config/api_key")
if DST.exists():
    print("BLOCKED: target exists, refusing to overwrite"); sys.exit(1)
key = None
for line in SRC.read_text().splitlines():
    if line.startswith("CUBE_API_KEY="):
        key = line.split("=", 1)[1].strip().strip('"').strip("'")
if not (key and 0 < len(key) <= 4096 and all(33 <= ord(c) <= 126 for c in key)):
    print("BLOCKED: key not found or invalid in controlled source"); sys.exit(1)
parent = DST.parent
assert parent.stat().st_uid == 0 and oct(parent.stat().st_mode & 0o777) == "0o700"
raw = (key + "\n").encode()  # 合同：整个文件即 key 字节（read 端 decode ascii）；行尾换行保留与否以最终读取器为准——采用纯 key+换行
fd = os.open(DST, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
try:
    os.write(fd, raw); os.fsync(fd)
finally:
    os.close(fd)
print("materialized:", DST, "mode:", oct(os.stat(DST).st_mode & 0o777), "len:", len(raw),
      "sha256:", hashlib.sha256(raw).hexdigest())
