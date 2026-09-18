"""Explicit node-side CSV broker entry; disabled without pinned approved config."""

import argparse
import ipaddress
import os
import ssl
import sys

from .business_broker import BusinessBroker, load_approved_permits
from .business_cube_session import ApprovedCsvSessionFactory
from .business_http import make_tls_server
from .business_lifecycle_provider import LifecycleBusinessProvider
from .business_workspace import CsvWorkspace
from .csv_business import BusinessStore
from .m3_create_process import _path, _pinned
from .m3_probe_process import _decode, config_bytes


def load_config(path, digest):
    config = _decode(_pinned(path, digest))
    fields = {"schema", "approved", "bind_host", "bind_port", "certificate_file", "private_key_file",
              "permits_file", "permits_sha256", "store_directory", "workspace_directory", "plans"}
    if (set(config) != fields or type(config["schema"]) is not int or config["schema"] != 1
            or config["approved"] is not True):
        raise ValueError("unapproved service configuration")
    address = ipaddress.ip_address(config["bind_host"])
    if (address.version != 4 or address.is_unspecified or address.is_multicast
            or not (address.is_private or address.is_loopback)
            or type(config["bind_port"]) is not int or not 1024 <= config["bind_port"] <= 65535):
        raise ValueError("explicit private IPv4 listener required")
    for key in ("certificate_file", "private_key_file"):
        config_bytes(_path(config[key]))
    permits = load_approved_permits(_path(config["permits_file"]), config["permits_sha256"])
    plans = {}
    for plan in config["plans"]:
        if set(plan) != {"owner_id", "request_id", "lifecycle_file", "lifecycle_sha256", "business_file", "business_sha256"}:
            raise ValueError("invalid plan reference")
        key = (plan["owner_id"], plan["request_id"])
        if key in plans:
            raise ValueError("duplicate plan")
        _pinned(_path(plan["lifecycle_file"]), plan["lifecycle_sha256"])
        _pinned(_path(plan["business_file"]), plan["business_sha256"])
        plans[key] = plan
    if set(plans) != {(p.request.owner_id, p.request.request_id) for p in permits} or not plans:
        raise ValueError("permit/plan mismatch")
    return config, permits, plans


def serve(path, digest):
    if sys.platform != "linux" or os.getuid() != 0 or os.geteuid() != 0:
        raise ValueError("same-host Linux node installation required")
    config, permits, plans = load_config(path, digest)
    def provider_for(permit):
        plan = plans[(permit.request.owner_id, permit.request.request_id)]
        return LifecycleBusinessProvider(lifecycle_file=_path(plan["lifecycle_file"]),
            lifecycle_sha256=plan["lifecycle_sha256"],
            session_for=ApprovedCsvSessionFactory(_path(plan["business_file"]), plan["business_sha256"]))
    broker = BusinessBroker(permits=permits, store=BusinessStore(_path(config["store_directory"])),
        workspace=CsvWorkspace(_path(config["workspace_directory"])), provider_for=provider_for)
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.minimum_version = ssl.TLSVersion.TLSv1_2
    tls.load_cert_chain(config["certificate_file"], config["private_key_file"])
    server = make_tls_server((config["bind_host"], config["bind_port"]), tls_context=tls, broker=broker)
    try:
        server.serve_forever()
    finally:
        server.server_close()


def main():
    try:
        if not sys.flags.isolated or not sys.argv[1:]:
            sys.exit(2)
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument("--config", required=True)
        parser.add_argument("--sha256", required=True)
        args = parser.parse_args()
        serve(_path(args.config), args.sha256)
    except Exception:
        sys.exit(2)  # Never expose config contents or credential-bearing errors.


if __name__ == "__main__":
    main()
