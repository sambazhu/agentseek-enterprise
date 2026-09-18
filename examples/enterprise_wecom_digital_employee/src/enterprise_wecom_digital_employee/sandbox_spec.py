"""Opt-in gateway spec for the remote CSV pilot, not the default gateway spec."""

import os


def build_spec():
    """Load pinned gateway composition; Cube credentials remain on the node."""
    from agentseek_execution.business_http import BusinessHttpClient
    from agentseek_execution.csv_business import BusinessStore
    from agentseek_execution.m3_create_process import _path, _pinned
    from agentseek_execution.m3_probe_process import _decode, config_bytes
    from agentseek_files.settings import FilesSettings
    from agentseek_files.store import LocalFileStore

    from .agent import build_spec as build_agent_spec
    from .sandbox_authorization import ApprovedGrantCatalog
    from .sandbox_remote import RemoteCsvRunner, remote_csv_tools

    path = _path(os.environ["AGENTSEEK_SANDBOX_BUSINESS_CONFIG"])
    digest = os.environ["AGENTSEEK_SANDBOX_BUSINESS_CONFIG_SHA256"]
    config = _decode(_pinned(path, digest))
    if (set(config) != {"schema", "approved", "endpoint", "ca_file", "broker_token_file",
                       "grants_file", "grants_sha256", "mirror_directory"}
            or type(config["schema"]) is not int or config["schema"] != 1 or config["approved"] is not True):
        raise ValueError("unapproved sandbox gateway configuration")
    client = BusinessHttpClient(endpoint=config["endpoint"], ca_file=_path(config["ca_file"]),
        token=config_bytes(_path(config["broker_token_file"])).decode("ascii"))
    grants = ApprovedGrantCatalog(_path(config["grants_file"]), config["grants_sha256"])
    store = BusinessStore(_path(config["mirror_directory"]))
    tools = remote_csv_tools(grant_for=grants, file_store=LocalFileStore(FilesSettings.from_env()),
                             runner=RemoteCsvRunner(client=client, store=store))
    return build_agent_spec(sandbox_tools=tools)
