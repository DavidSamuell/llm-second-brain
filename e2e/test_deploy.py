"""SPEC §16.2: with no proxy, no service is reachable from another machine."""

import socket
import subprocess
import threading
import uuid
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx
import pytest
from harness import Stack, host_ip


def _probe(network: str, ip: str, port: int) -> bool:
    """Try a TCP connect from a container on its own network, i.e. from 'another machine'."""
    result = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--network",
            network,
            "alpine:3",
            "nc",
            "-z",
            "-w",
            "3",
            ip,
            str(port),
        ],
        capture_output=True,
        check=False,
    )
    return result.returncode == 0


@pytest.fixture(scope="module")
def outside_network() -> Iterator[str]:
    name = f"vault-e2e-outside-{uuid.uuid4().hex[:8]}"
    subprocess.run(["docker", "network", "create", name], check=True, capture_output=True)
    yield name
    subprocess.run(["docker", "network", "rm", name], check=False, capture_output=True)


@pytest.fixture(scope="module")
def control_listener() -> Iterator[int]:
    """A service deliberately bound to all interfaces, to prove the probe can see the host."""

    class Ok(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(204)
            self.end_headers()

        def log_message(self, *args: object) -> None:
            pass

    server = HTTPServer(("0.0.0.0", 0), Ok)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server.server_port
    server.shutdown()


def test_services_answer_on_loopback(stack: Stack) -> None:
    assert httpx.get(f"{stack.mcp_url}/health").status_code == 200
    assert httpx.get(stack.ignis_url).status_code == 200


def _connects(ip: str, port: int) -> bool:
    try:
        with socket.create_connection((ip, port), timeout=3):
            return True
    except OSError:
        return False


def test_16_2_not_reachable_on_the_hosts_network_address(
    stack: Stack, control_listener: int
) -> None:
    """Connect to this host's LAN address, where another machine would. Docker's DNAT rules
    also apply to locally generated traffic, so a port published on 0.0.0.0 would answer."""
    ip = host_ip()
    assert not ip.startswith("127.")
    assert _connects(ip, control_listener), "control listener unreachable on the LAN address"
    for port_var in ("IGNIS_PORT", "MCP_PORT"):
        assert not _connects(ip, int(stack.env[port_var])), f"{port_var} is reachable on {ip}"


def test_16_2_not_reachable_from_another_network(
    stack: Stack, outside_network: str, control_listener: int
) -> None:
    ip = host_ip()
    if not _probe(outside_network, ip, control_listener):
        pytest.skip(
            "this host's firewall blocks container-to-host traffic; LAN-address test covers binding"
        )
    for port_var in ("IGNIS_PORT", "MCP_PORT"):
        assert not _probe(outside_network, ip, int(stack.env[port_var])), f"{port_var} is exposed"


def test_published_ports_are_bound_to_loopback_only(stack: Stack) -> None:
    out = stack.compose("ps", "--format", "{{.Ports}}").stdout
    published = [p for p in out.replace(",", "\n").split() if "->" in p]
    assert published
    assert all(p.startswith("127.0.0.1:") for p in published), published
