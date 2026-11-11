"""Per-credential rate limits (SPEC §6.5, §16.8, plan D19).

Sliding windows in memory (vault-mcp runs one worker). Only accepted calls count, so a
client hammering a limit doesn't push its own window forward. Five or more refusals for one
credential within ten minutes send one alert, at most once per credential per hour.
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque
from collections.abc import Callable
from typing import Any, Protocol

from vault_core.policy import RateLimits
from vault_mcp.errors import VaultToolError

MINUTE, DAY = 60.0, 86400.0
HITS_FOR_ALERT, HIT_WINDOW, ALERT_EVERY = 5, 600.0, 3600.0


class AlertSink(Protocol):
    def send(
        self,
        event: str,
        severity: str,
        message: str,
        details: dict[str, Any] | None = None,
        *,
        subject: str = "",
    ) -> bool: ...


def _in_thread(send: Callable[[], object]) -> None:
    threading.Thread(target=send, name="alert", daemon=True).start()


class RateLimiter:
    def __init__(
        self,
        limits: RateLimits,
        *,
        alerts: AlertSink,
        clock: Callable[[], float] = time.monotonic,
        dispatch: Callable[[Callable[[], object]], None] | None = None,
    ) -> None:
        self.limits = limits
        self.alerts = alerts
        self.clock = clock
        self._requests: dict[str, deque[float]] = defaultdict(deque)
        self._writes: dict[str, deque[float]] = defaultdict(deque)
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._alerted: dict[str, float] = {}
        self._lock = threading.Lock()
        # check() runs on the event loop: webhook delivery (up to 5 s) goes to its own thread.
        self._dispatch = dispatch or _in_thread

    @staticmethod
    def _retry_after(window: deque[float], now: float, span: float, limit: int) -> float | None:
        """Seconds until the window has room again, or None if it has room now."""
        while window and now - window[0] >= span:
            window.popleft()
        if len(window) < limit:
            return None
        return window[len(window) - limit] + span - now

    def check(self, credential_id: str, *, write: bool) -> None:
        """Count one call, or raise `rate_limited` (with `retry_after_s`) without counting it."""
        with self._lock:
            now = self.clock()
            requests, writes = self._requests[credential_id], self._writes[credential_id]
            waits = [self._retry_after(requests, now, MINUTE, self.limits.requests_per_minute)]
            if write:
                minute = deque(t for t in writes if now - t < MINUTE)
                waits.append(self._retry_after(minute, now, MINUTE, self.limits.writes_per_minute))
                waits.append(self._retry_after(writes, now, DAY, self.limits.writes_per_day))
            blocking = [w for w in waits if w is not None]
            if blocking:
                self._record_hit(credential_id, now)
                raise VaultToolError(
                    "rate_limited", "rate limit reached for this credential; retry later",
                    retry_after_s=round(max(blocking), 3),
                )  # fmt: skip
            requests.append(now)
            if write:
                writes.append(now)

    def _record_hit(self, credential_id: str, now: float) -> None:
        hits = self._hits[credential_id]
        hits.append(now)
        while hits and now - hits[0] >= HIT_WINDOW:
            hits.popleft()
        last = self._alerted.get(credential_id)
        if len(hits) >= HITS_FOR_ALERT and (last is None or now - last >= ALERT_EVERY):
            self._alerted[credential_id] = now
            message = f"A credential hit its rate limit {len(hits)} times in 10 minutes."
            self._dispatch(
                lambda: self.alerts.send(
                    "rate_limit_repeated",
                    "warning",
                    message,
                    {"credential_id": credential_id},
                    subject=credential_id,
                )
            )
