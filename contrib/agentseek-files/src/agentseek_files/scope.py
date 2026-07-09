from __future__ import annotations

import hashlib
import hmac


def hmac_key(value: str, *, secret: str, prefix: str = "hmac") -> str:
    normalized = str(value or "").strip()
    if not normalized:
        return f"{prefix}-empty"
    digest = hmac.new(secret.encode("utf-8"), normalized.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{prefix}-{digest[:24]}"
