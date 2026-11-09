"""Service settings shared by vault-mcp and vault-watcher (plan D27)."""

from __future__ import annotations

import ipaddress
from pathlib import Path

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Container-internal paths; compose sets them, deployers normally don't."""

    vault_dir: Path = Path("/vault")
    data_dir: Path = Path("/data")
    policy_file: Path = Path("/config/vault-mcp.yaml")
    bind_addr: str = "127.0.0.1"
    history_ack_timeout: float = 5.0  # D21: how long a write waits for vault-watcher


def bind_addr_warning(bind_addr: str) -> str | None:
    """§5.4: services warn at startup when published beyond loopback."""
    try:
        if ipaddress.ip_address(bind_addr).is_loopback:
            return None
    except ValueError:
        pass
    return (
        f"BIND_ADDR={bind_addr}: service ports are reachable beyond this host. "
        "Put an authenticating ingress in front (SPEC §5.1) or bind to 127.0.0.1."
    )
