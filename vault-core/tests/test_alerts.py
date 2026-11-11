import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import ClassVar

import pytest

from vault_core.alerts import AlertSender, build_payload


class _Stub(BaseHTTPRequestHandler):
    received: ClassVar[list[tuple[str, dict[str, object]]]] = []

    def do_POST(self) -> None:
        body = self.rfile.read(int(self.headers["Content-Length"]))
        type(self).received.append((self.headers["Content-Type"], json.loads(body)))
        self.send_response(204)
        self.end_headers()

    def log_message(self, *args: object) -> None:
        pass


@pytest.fixture
def stub() -> Iterator[tuple[str, list[tuple[str, dict[str, object]]]]]:
    _Stub.received = []
    server = HTTPServer(("127.0.0.1", 0), _Stub)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}/hook", _Stub.received
    server.shutdown()


def test_generic_webhook_receives_structured_json(stub: tuple[str, list]) -> None:
    url, received = stub
    sender = AlertSender(url, service="vault-watcher")
    assert sender.send("secret_detected", "high", "Secret in wiki/page.md", {"rule": "jwt"})
    ((content_type, body),) = received
    assert content_type == "application/json"
    assert body["event"] == "secret_detected"
    assert body["severity"] == "high"
    assert body["service"] == "vault-watcher"
    assert body["message"] == "Secret in wiki/page.md"
    assert body["details"] == {"rule": "jwt"}
    assert str(body["ts"]).endswith("Z")


def test_same_event_and_subject_is_deduplicated_within_the_window(stub: tuple[str, list]) -> None:
    url, received = stub
    now = [1000.0]
    sender = AlertSender(url, service="s", dedupe_window_s=3600, clock=lambda: now[0])
    assert sender.send("rate_limited", "warning", "m", subject="alice")
    assert not sender.send("rate_limited", "warning", "m", subject="alice")
    assert sender.send("rate_limited", "warning", "m", subject="bob")
    now[0] += 3601
    assert sender.send("rate_limited", "warning", "m", subject="alice")
    assert len(received) == 3


def test_delivery_failure_returns_false_and_does_not_raise() -> None:
    sender = AlertSender("http://127.0.0.1:9/unreachable", service="s", timeout_s=0.5)
    assert sender.send("disk", "warning", "Disk at 81%") is False


def test_no_webhook_configured_is_a_quiet_no_op() -> None:
    assert AlertSender(None, service="s").send("disk", "warning", "x") is False


@pytest.mark.parametrize(
    ("url", "key"),
    [
        ("https://hooks.slack.com/services/T1/B1/x", "text"),
        ("https://discord.com/api/webhooks/1/abc", "content"),
        ("https://discordapp.com/api/webhooks/1/abc", "content"),
    ],
)
def test_chat_webhooks_get_their_native_shape(url: str, key: str) -> None:
    payload = build_payload(url, "git-sync", "git_conflict", "high", "Conflict", {"file": "a.md"})
    assert set(payload) == {key}
    assert "git-sync" in str(payload[key])
    assert "Conflict" in str(payload[key])
