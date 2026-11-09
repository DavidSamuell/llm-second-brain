"""`/data/audit.log`: JSON Lines, one entry per tool call or recovery action (D20).

Each line is written with a single write() on an O_APPEND descriptor, which keeps lines from
vault-mcp and vault-watcher intact when both append at once on a local filesystem.
"""

from __future__ import annotations

import json
import os
import secrets
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_last_ms = 0
_last_rand = 0


def new_id() -> str:
    """ULID: 48-bit ms timestamp + 80-bit randomness, monotonic within one process."""
    global _last_ms, _last_rand
    ms = time.time_ns() // 1_000_000
    if ms <= _last_ms:
        ms, rand = _last_ms, _last_rand + 1
    else:
        rand = secrets.randbits(80)
    _last_ms, _last_rand = ms, rand
    value = (ms << 80) | (rand & ((1 << 80) - 1))
    return "".join(_CROCKFORD[(value >> shift) & 31] for shift in range(125, -1, -5))


def utc_timestamp() -> str:
    """RFC 3339 UTC with millisecond precision, e.g. 2026-11-09T09:30:00.123Z."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


class AuditLog:
    def __init__(self, path: Path) -> None:
        self.path = path

    def append(
        self,
        *,
        service: str,
        tool: str,
        result: str,
        id: str | None = None,
        durable: bool = False,
        **fields: Any,
    ) -> str:
        """Append one entry. `durable=True` fsyncs it (D21 step 4: before the commit rename)."""
        entry_id = id or new_id()
        entry = {
            "ts": utc_timestamp(),
            "id": entry_id,
            "service": service,
            "tool": tool,
            "result": result,
        }
        entry.update({k: v for k, v in fields.items() if v is not None})
        line = (json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o640)
        try:
            written = os.write(fd, line)
            if durable:
                os.fsync(fd)
        finally:
            os.close(fd)
        if written != len(line):
            raise OSError(f"short write to {self.path}: {written} of {len(line)} bytes")
        return entry_id

    def has_id(self, entry_id: str) -> bool:
        """Whether an entry with this id exists. Linear scan: only used for crash recovery."""
        needle = f'"id":"{entry_id}"'
        try:
            with self.path.open() as f:
                return any(needle in line and json.loads(line).get("id") == entry_id for line in f)
        except FileNotFoundError:
            return False
