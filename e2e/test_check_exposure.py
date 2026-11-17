"""SPEC §16.4: check-exposure passes behind a correct proxy and fails clearly without one."""

import datetime
import http.client
import ipaddress
import os
import ssl
import subprocess
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from harness import REPO, Stack, free_port


def _tls_files(tmp: Path) -> tuple[Path, Path, Path]:
    """A throwaway CA and a certificate for 127.0.0.1 signed by it."""
    now = datetime.datetime.now(datetime.UTC)
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "e2e test CA")])
    ca = (
        x509.CertificateBuilder().subject_name(ca_name).issuer_name(ca_name).public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number()).not_valid_before(now).not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True).sign(ca_key, hashes.SHA256())
    )  # fmt: skip
    key = ec.generate_private_key(ec.SECP256R1())
    cert = (
        x509.CertificateBuilder().subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")]))
        .issuer_name(ca_name).public_key(key.public_key()).serial_number(x509.random_serial_number())
        .not_valid_before(now).not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), critical=False)
        .sign(ca_key, hashes.SHA256())
    )  # fmt: skip
    ca_pem, cert_pem, key_pem = tmp / "ca.pem", tmp / "cert.pem", tmp / "key.pem"
    ca_pem.write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    cert_pem.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_pem.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return ca_pem, cert_pem, key_pem


def _serve_tls(
    port: int, handler: type[BaseHTTPRequestHandler], cert: Path, key: Path
) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


@pytest.fixture
def proxies(stack: Stack, tmp_path: Path) -> Iterator[tuple[str, str, Path]]:
    """A stand-in for a correct ingress: the editor demands a login, the MCP host is passed
    through to vault-mcp untouched."""
    ca, cert, key = _tls_files(tmp_path)
    mcp_port = int(stack.env["MCP_PORT"])

    class LoginWall(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(302)
            self.send_header("Location", "https://login.example/authorize")
            self.end_headers()

        def log_message(self, *args: object) -> None:
            pass

    class PassThrough(BaseHTTPRequestHandler):
        def _forward(self) -> None:
            body = self.rfile.read(int(self.headers.get("Content-Length", 0) or 0))
            upstream = http.client.HTTPConnection("127.0.0.1", mcp_port, timeout=10)
            headers = {
                k: v for k, v in self.headers.items() if k.lower() not in ("host", "content-length")
            }
            upstream.request(self.command, self.path, body=body or None, headers=headers)
            resp = upstream.getresponse()
            data = resp.read()
            self.send_response(resp.status)
            for k, v in resp.getheaders():
                if k.lower() not in ("transfer-encoding", "content-length", "connection"):
                    self.send_header(k, v)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        do_GET = do_POST = _forward

        def log_message(self, *args: object) -> None:
            pass

    ignis_port = free_port()
    servers = [
        _serve_tls(ignis_port, LoginWall, cert, key),
        _serve_tls(int(stack.env["E2E_MCP_PROXY_PORT"]), PassThrough, cert, key),
    ]
    yield (
        f"https://127.0.0.1:{ignis_port}",
        f"https://127.0.0.1:{stack.env['E2E_MCP_PROXY_PORT']}",
        ca,
    )
    for server in servers:
        server.shutdown()


def check(stack: Stack, ignis: str, mcp: str, **env: str) -> subprocess.CompletedProcess[str]:
    run_env = {
        **os.environ,
        "IGNIS_PORT": stack.env["IGNIS_PORT"],
        "MCP_PORT": stack.env["MCP_PORT"],
        **env,
    }
    return subprocess.run([str(REPO / "scripts" / "check-exposure"), ignis, mcp], env=run_env,
                          capture_output=True, text=True, timeout=120, check=False)  # fmt: skip


def test_16_4_passes_behind_a_proxy_that_meets_the_contract(
    stack: Stack, proxies: tuple[str, str, Path]
) -> None:
    ignis, mcp, ca = proxies
    token = stack.token(
        "create", "--name", "exposure-check", "--owner", "ops@example.com", "--scope", "read"
    )
    r = check(stack, ignis, mcp, CHECK_TOKEN=token, CHECK_CA_FILE=str(ca), CHECK_PUBLIC_IPS=" ")
    assert r.returncode == 0, r.stdout
    assert r.stdout.count("PASS") == 7
    assert "SKIP  raw-ports" in r.stdout
    assert token not in r.stdout + r.stderr


def test_16_4_fails_clearly_for_an_exposed_editor_and_plain_http(stack: Stack) -> None:
    ignis = f"http://127.0.0.1:{stack.env['IGNIS_PORT']}"  # the editor itself, no proxy, no TLS
    mcp = f"http://127.0.0.1:{stack.env['MCP_PORT']}"
    r = check(stack, ignis, mcp, CHECK_PUBLIC_IPS="127.0.0.1")
    failed = [line for line in r.stdout.splitlines() if line.startswith("FAIL")]
    assert r.returncode == len(failed)
    assert any("not HTTPS" in line and ignis in line for line in failed)
    assert any("serves the editor without login" in line for line in failed)
    assert any(f"127.0.0.1:{stack.env['IGNIS_PORT']} is reachable" in line for line in failed)
