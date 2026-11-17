"""D10 tool error contract: `ToolError` whose text is JSON `{code, message, details}`."""

from __future__ import annotations

import json
from typing import Any, Literal

from fastmcp.exceptions import ToolError

ErrorCode = Literal[
    "conflict",
    "already_claimed",
    "not_claimed",
    "forbidden",
    "not_found",
    "rate_limited",
    "secret_detected",
    "too_large",
    "invalid",
    "ambiguous_heading",
    "history_pending",
]


class VaultToolError(ToolError):
    def __init__(self, code: ErrorCode, message: str, **details: Any) -> None:
        super().__init__(json.dumps({"code": code, "message": message, "details": details}))
        self.code: ErrorCode = code
