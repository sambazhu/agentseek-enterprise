#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os

from agentseek_wecom.outbound import (
    UnsupportedWeComOutbound,
    outbound_capabilities,
    require_outbound_message_type,
    validate_artifact_download_base_url,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Inspect the configured WeCom outbound delivery capability.")
    parser.add_argument(
        "--transport-mode",
        choices=("callback", "long_connection"),
        default=os.environ.get("AGENTSEEK_WECOM_TRANSPORT_MODE", "callback"),
    )
    parser.add_argument(
        "--artifact-delivery-mode",
        choices=("disabled", "signed_link", "direct_file"),
        default=os.environ.get("AGENTSEEK_WORK_ARTIFACT_DELIVERY_MODE", "disabled"),
    )
    parser.add_argument(
        "--download-base-url",
        default=os.environ.get("AGENTSEEK_WORK_ARTIFACT_PUBLIC_BASE_URL", ""),
    )
    args = parser.parse_args(argv)

    capabilities = outbound_capabilities(args.transport_mode)
    result: dict[str, object] = {
        "transport": capabilities.as_dict(),
        "artifact_delivery_mode": args.artifact_delivery_mode,
        "configuration_ready": False,
    }
    errors: list[str] = []

    if not capabilities.implemented:
        errors.append(f"transport {args.transport_mode!r} is documented but not implemented")
    elif args.artifact_delivery_mode == "disabled":
        result["decision"] = "Artifact delivery is disabled until M4 publication and delivery ledgers exist."
    elif args.artifact_delivery_mode == "direct_file":
        try:
            require_outbound_message_type(args.transport_mode, "file")
        except UnsupportedWeComOutbound as exc:
            errors.append(str(exc))
        else:
            result["configuration_ready"] = True
    else:
        if not args.download_base_url:
            errors.append("signed_link delivery requires AGENTSEEK_WORK_ARTIFACT_PUBLIC_BASE_URL")
        else:
            try:
                result["download_base_url"] = validate_artifact_download_base_url(args.download_base_url)
            except ValueError as exc:
                errors.append(str(exc))
            else:
                require_outbound_message_type(args.transport_mode, "template_card")
                result["configuration_ready"] = True
                result["decision"] = "Deliver a short-lived signed HTTPS link in an AI Bot template card."

    if errors:
        result["errors"] = errors
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
