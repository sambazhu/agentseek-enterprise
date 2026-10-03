"""冲突证据核对（有界）：声明“除已登记主体外无其他 root/API key 持有者”——检查本机是否存在
其他 key 副本/异常持有途径。只输出文件路径与计数，绝不输出 key 正文。"""
import json, os, subprocess
from pathlib import Path
key = None
for line in Path("/root/cube-one-click-v0.7.0/poc-install.env").read_text().splitlines():
    if line.startswith("CUBE_API_KEY="):
        key = line.split("=", 1)[1].strip()
assert key
findings = {"key_literal_copies": [], "env_files_scanned": 0}
for base in ("/root", "/etc", "/opt", "/home", "/tmp", "/var/lib"):
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [d for d in dirnames if d not in (".git", "node_modules", "__pycache__", "venv", "venv-test", "venv-runtime", "venv-verify")]
        if dirpath.count(os.sep) - base.count(os.sep) > 4:
            dirnames[:] = []
        for fn in filenames:
            if fn.endswith((".env", ".key", "api_key", "poc-install.env")):
                p = Path(dirpath) / fn
                try:
                    findings["env_files_scanned"] += 1
                    if p.stat().st_size < 65536 and key.encode() in p.read_bytes():
                        findings["key_literal_copies"].append(str(p))
                except OSError:
                    pass
    # cap runtime
print(json.dumps({"env_like_files_scanned": findings["env_files_scanned"],
                  "literal_key_copy_paths": findings["key_literal_copies"],
                  "note": "仅统计包含 key 字面量的副本；进程环境/内存中的持有无法从本机枚举"}, indent=2))
