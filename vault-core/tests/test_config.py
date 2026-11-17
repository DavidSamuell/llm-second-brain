from pathlib import Path

import pytest

from vault_core.config import Settings, bind_addr_warning


def test_settings_default_to_container_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("VAULT_DIR", "DATA_DIR", "POLICY_FILE", "BIND_ADDR"):
        monkeypatch.delenv(var, raising=False)
    s = Settings()
    assert (s.vault_dir, s.data_dir, s.policy_file, s.bind_addr) == (
        Path("/vault"),
        Path("/data"),
        Path("/config/vault-mcp.yaml"),
        "127.0.0.1",
    )


def test_settings_read_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("VAULT_DIR", str(tmp_path / "v"))
    monkeypatch.setenv("BIND_ADDR", "0.0.0.0")
    s = Settings()
    assert s.vault_dir == tmp_path / "v"
    assert s.bind_addr == "0.0.0.0"


@pytest.mark.parametrize(
    ("addr", "warns"),
    [("127.0.0.1", False), ("::1", False), ("0.0.0.0", True), ("::", True), ("192.0.2.10", True)],
)
def test_bind_addr_warning(addr: str, warns: bool) -> None:
    msg = bind_addr_warning(addr)
    assert (msg is not None) is warns
    if msg:
        assert addr in msg
