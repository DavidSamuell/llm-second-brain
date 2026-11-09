"""vault-watcher: reacts to every change in the vault (SPEC §7, plan D21/D22).

One worker thread owns all history writes and always holds the vault lock while writing.
watchdog threads only queue paths; the spool directory wakes the worker for MCP writes.
"""

from __future__ import annotations

import fcntl
import logging
import os
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from vault_core.audit import AuditLog
from vault_core.history import Source
from vault_core.lock import lock_path, read_owner, vault_lock
from vault_watcher.recorder import AlertSink, Recorder, is_tracked

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class WatcherConfig:
    vault_dir: Path
    data_dir: Path
    debounce_quiet_s: float = 5.0
    debounce_cap_s: float = 60.0
    scan_interval_s: float = 600.0
    heartbeat_interval_s: float = 5.0
    gc_interval_s: float = 86400.0
    retention_days: int = 90
    min_versions: int = 10
    health_port: int | None = None


def lock_holder(data_dir: Path) -> str | None:
    """Who holds the vault lock right now, or None if it is free (non-blocking probe)."""
    path = lock_path(data_dir)
    try:
        fd = os.open(path, os.O_RDONLY)
    except FileNotFoundError:
        return None
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            return read_owner(data_dir)
        fcntl.flock(fd, fcntl.LOCK_UN)
        return None
    finally:
        os.close(fd)


class _Pending:
    """Debounce: a path is due once quiet for `quiet` seconds, or `cap` seconds after its
    first event, whichever comes first. A git-sync event marks the whole burst as git."""

    def __init__(self, quiet: float, cap: float) -> None:
        self._quiet, self._cap = quiet, cap
        self._items: dict[str, tuple[float, float, Source]] = {}
        self._lock = threading.Lock()

    def add(self, rel: str, source: Source) -> None:
        now = time.monotonic()
        with self._lock:
            first, _, prev = self._items.get(rel, (now, now, source))
            self._items[rel] = (first, now, "git" if "git" in (source, prev) else source)

    def due(self) -> list[tuple[str, Source]]:
        now = time.monotonic()
        with self._lock:
            ready = [
                (rel, src)
                for rel, (first, last, src) in self._items.items()
                if now - last >= self._quiet or now - first >= self._cap
            ]
            for rel, _ in ready:
                del self._items[rel]
        return ready


class Watcher:
    def __init__(self, config: WatcherConfig, *, alerts: AlertSink) -> None:
        self.config = config
        self.alerts = alerts
        self.recorder = Recorder(
            config.vault_dir, config.data_dir, AuditLog(config.data_dir / "audit.log"), alerts
        )
        self.heartbeat = config.data_dir / "vault-watcher" / "heartbeat"
        self._pending = _Pending(config.debounce_quiet_s, config.debounce_cap_s)
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._observer = Observer()
        self._thread: threading.Thread | None = None
        self._health: ThreadingHTTPServer | None = None

    # -- lifecycle -------------------------------------------------------------------------

    def start(self) -> None:
        vault, inbox = self.config.vault_dir, self.recorder.spool.inbox
        inbox.mkdir(parents=True, exist_ok=True)
        self.heartbeat.parent.mkdir(parents=True, exist_ok=True)
        self._observer.schedule(_VaultEvents(self), str(vault), recursive=True)
        self._observer.schedule(_InboxEvents(self._wake), str(inbox), recursive=False)
        self._observer.start()  # before the startup scan, so nothing slips between the two
        with vault_lock(self.config.data_dir, "vault-watcher"):
            drained = self.recorder.drain()
            changed = self.recorder.scan()
        log.info("startup: %d spool entries resolved, %d files recorded", drained, changed)
        self.heartbeat.touch()
        if self.config.health_port is not None:
            self._health = _health_server(
                self.config.health_port, self.heartbeat, self.config.heartbeat_interval_s
            )
        self._thread = threading.Thread(target=self._run, name="vault-watcher", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        self._observer.stop()
        self._observer.join(5)
        if self._thread:
            self._thread.join(10)
        if self._health:
            self._health.shutdown()

    def wait(self) -> None:
        while self._thread and self._thread.is_alive():
            self._thread.join(1)

    # -- worker ----------------------------------------------------------------------------

    def _run(self) -> None:
        cfg = self.config
        next_beat = next_scan = time.monotonic()
        next_scan += cfg.scan_interval_s
        next_gc = time.monotonic() + cfg.gc_interval_s
        while not self._stop.is_set():
            now = time.monotonic()
            if now >= next_beat:
                self.heartbeat.touch()
                next_beat = now + cfg.heartbeat_interval_s
            if self._wake.wait(0.05):
                self._wake.clear()
                if self._stop.is_set():
                    break
                self._locked(self.recorder.drain)
            for rel, source in self._pending.due():
                self._locked(
                    lambda r=rel, s=source: self.recorder.drain() + self.recorder.snapshot(r, s)
                )
            if now >= next_scan:
                self._locked(lambda: self.recorder.drain() + self.recorder.scan())
                next_scan = now + cfg.scan_interval_s
            if now >= next_gc:
                self._locked(
                    lambda: self.recorder.history.gc(
                        now=datetime.now(UTC),
                        retention_days=cfg.retention_days,
                        min_versions=cfg.min_versions,
                    )
                )
                next_gc = now + cfg.gc_interval_s

    def _locked(self, work: object) -> None:
        try:
            with vault_lock(self.config.data_dir, "vault-watcher"):
                work()  # type: ignore[operator]
        except Exception:
            log.exception("history work failed; will retry on the next event or scan")

    def queue(self, rel: str) -> None:
        if is_tracked(rel):
            source: Source = "git" if lock_holder(self.config.data_dir) == "git-sync" else "web"
            self._pending.add(rel, source)


class _VaultEvents(FileSystemEventHandler):
    def __init__(self, watcher: Watcher) -> None:
        self._w = watcher
        self._root = watcher.config.vault_dir

    def on_any_event(self, event: FileSystemEvent) -> None:
        if event.is_directory or event.event_type in ("opened", "closed_no_write"):
            return
        for raw in (event.src_path, getattr(event, "dest_path", "")):
            if raw:
                path = Path(os.fsdecode(raw))
                if path.is_relative_to(self._root):
                    self._w.queue(path.relative_to(self._root).as_posix())


class _InboxEvents(FileSystemEventHandler):
    def __init__(self, wake: threading.Event) -> None:
        self._wake = wake

    def on_any_event(self, event: FileSystemEvent) -> None:
        self._wake.set()


def _health_server(port: int, heartbeat: Path, interval: float) -> ThreadingHTTPServer:
    class Health(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            fresh = heartbeat.exists() and time.time() - heartbeat.stat().st_mtime < max(
                30.0, 6 * interval
            )
            ok = self.path == "/health" and fresh
            self.send_response(200 if ok else 503)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"status":"ok"}' if ok else b'{"status":"unhealthy"}')

        def log_message(self, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("0.0.0.0", port), Health)  # noqa: S104 - inside the container network
    threading.Thread(target=server.serve_forever, name="health", daemon=True).start()
    return server
