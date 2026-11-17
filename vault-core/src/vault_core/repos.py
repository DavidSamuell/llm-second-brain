"""Repository identities shared by project lookup and session capture hooks."""

from __future__ import annotations

import re
from urllib.parse import unquote, urlsplit

_SCP = re.compile(r"^(?:[^/@:\s]+@)?(\[[^\]]+\]|[^/:\s]+):(.+)$")
_DEFAULT_PORTS = {"https": 443, "http": 80, "ssh": 22, "git": 9418}


def normalise_remote(url: str) -> str | None:
    """SSH/HTTPS/canonical remotes -> host/path, never credentials or local paths.

    GitHub paths are case-insensitive; other hosts retain their path's case.
    Nonstandard ports are part of self-hosted repository identity.
    """
    value = url.strip()
    if not value or any(c.isspace() or ord(c) < 32 for c in value) or "\\" in value:
        return None
    if value.startswith(("/", ".", "~")):
        return None
    if "://" not in value:
        scp = _SCP.fullmatch(value)
        if scp is not None and "/" not in scp[1] and not scp[2].split("/", 1)[0].isdigit():
            value = "ssh://" + scp[1] + "/" + scp[2].lstrip("/")
        else:
            host = value.split("/", 1)[0].split(":", 1)[0]
            if "/" not in value or not ("." in host or host == "localhost" or host.startswith("[")):
                return None
            value = "https://" + value
    try:
        parsed = urlsplit(value)
        if parsed.scheme.lower() not in _DEFAULT_PORTS or not parsed.hostname:
            return None
        if parsed.query or parsed.fragment:
            return None
        host = parsed.hostname.lower()
        port = parsed.port
    except ValueError:
        return None
    if ":" in host:
        host = "[" + host + "]"
    if port is not None and port != _DEFAULT_PORTS[parsed.scheme.lower()]:
        host += f":{port}"
    path = unquote(parsed.path).strip("/")
    if path.lower().endswith(".git"):
        path = path[:-4]
    if not path or any(part in ("", ".", "..") for part in path.split("/")):
        return None
    if any(c.isspace() or ord(c) < 32 for c in path) or "\\" in path:
        return None
    if host == "github.com":
        if len(path.split("/")) != 2:
            return None
        path = path.lower()
    return f"{host}/{path}"
