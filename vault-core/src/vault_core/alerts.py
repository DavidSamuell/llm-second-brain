"""Alerts to `ALERT_WEBHOOK` (D25). Slack and Discord get their native shape; anything else
gets structured JSON. Delivery never raises: alerting must not break the operation."""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any
from urllib.parse import urlparse

from vault_core.audit import utc_timestamp

log = logging.getLogger(__name__)


def _text(service: str, severity: str, message: str, details: dict[str, Any] | None) -> str:
    text = f"[{severity}] {service}: {message}"
    if details:
        text += "\n" + "\n".join(f"• {k}: {v}" for k, v in details.items())
    return text


def build_payload(
    url: str,
    service: str,
    event: str,
    severity: str,
    message: str,
    details: dict[str, Any] | None,
) -> dict[str, Any]:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if host == "hooks.slack.com":
        return {"text": _text(service, severity, message, details)}
    if host in ("discord.com", "discordapp.com") and parsed.path.startswith("/api/webhooks"):
        return {"content": _text(service, severity, message, details)[:2000]}
    return {
        "event": event,
        "severity": severity,
        "service": service,
        "message": message,
        "details": details or {},
        "ts": utc_timestamp(),
    }


class AlertSender:
    def __init__(
        self,
        webhook_url: str | None,
        *,
        service: str,
        dedupe_window_s: float = 3600,
        timeout_s: float = 5,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._url = webhook_url or None
        self._service = service
        self._window = dedupe_window_s
        self._timeout = timeout_s
        self._clock = clock
        self._last_sent: dict[tuple[str, str], float] = {}

    def send(
        self,
        event: str,
        severity: str,
        message: str,
        details: dict[str, Any] | None = None,
        *,
        subject: str = "",
    ) -> bool:
        """Deliver once per (event, subject) per window. Returns whether it was delivered."""
        if self._url is None:
            log.warning("alert (no ALERT_WEBHOOK): %s %s", event, message)
            return False
        key, now = (event, subject), self._clock()
        last = self._last_sent.get(key)
        if last is not None and now - last < self._window:
            return False
        body = json.dumps(
            build_payload(self._url, self._service, event, severity, message, details)
        ).encode()
        request = urllib.request.Request(  # noqa: S310 - URL is the operator-configured webhook
            self._url, data=body, headers={"Content-Type": "application/json"}, method="POST"
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout):  # noqa: S310
                pass
        except (urllib.error.URLError, OSError, ValueError) as exc:
            log.error("alert delivery failed for %s: %s", event, exc)
            return False
        self._last_sent[key] = now
        return True
