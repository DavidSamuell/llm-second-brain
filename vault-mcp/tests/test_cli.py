import json
import re
from pathlib import Path

import pytest

from vault_mcp.cli import main
from vault_mcp.store import CredentialStore

TOKEN_RE = re.compile(r"vmcp_[A-Za-z0-9_-]{43}")


def run(capsys: pytest.CaptureFixture[str], data: Path, *argv: str) -> tuple[int, str, str]:
    code = main(["--data-dir", str(data), *argv])
    out, err = capsys.readouterr()
    return code, out, err


def test_create_prints_the_token_exactly_once(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    code, out, _ = run(
        capsys, tmp_path, "token", "create", "--name", "alice-laptop",
        "--owner", "alice@example.com", "--scope", "write", "--folders", "wiki/,raw/", "--expires", "30d",
    )  # fmt: skip
    assert code == 0
    tokens = TOKEN_RE.findall(out)
    assert len(tokens) == 1
    cred = CredentialStore(tmp_path / "tokens.db").verify(tokens[0])
    assert cred is not None
    assert (cred.scope, cred.folders) == ("write", ["wiki/", "raw/"])


def test_list_never_shows_tokens(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    _, out, _ = run(
        capsys,
        tmp_path,
        "token",
        "create",
        "--name",
        "a",
        "--owner",
        "a@example.com",
        "--scope",
        "read",
    )
    token = TOKEN_RE.findall(out)[0]
    code, listing, _ = run(capsys, tmp_path, "token", "list")
    assert code == 0
    assert "a@example.com" in listing
    assert token not in listing
    assert token[5:13] in listing  # display prefix identifies it


def test_list_json(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    run(
        capsys,
        tmp_path,
        "token",
        "create",
        "--name",
        "a",
        "--owner",
        "a@example.com",
        "--scope",
        "read",
        "--expires",
        "never",
    )
    _, out, _ = run(capsys, tmp_path, "token", "list", "--json")
    (row,) = json.loads(out)
    assert row["name"] == "a"
    assert row["expires_at"] is None
    assert row["status"] == "active"


def test_revoke_then_verify_fails(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    _, out, _ = run(
        capsys,
        tmp_path,
        "token",
        "create",
        "--name",
        "a",
        "--owner",
        "a@example.com",
        "--scope",
        "read",
    )
    token = TOKEN_RE.findall(out)[0]
    code, _, _ = run(capsys, tmp_path, "token", "revoke", "a")
    assert code == 0
    assert CredentialStore(tmp_path / "tokens.db").verify(token) is None


def test_errors_exit_non_zero_with_a_message(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    code, out, err = run(capsys, tmp_path, "token", "revoke", "ghost")
    assert code == 1
    assert "ghost" in err
    assert out == ""
    code, _, err = run(
        capsys,
        tmp_path,
        "token",
        "create",
        "--name",
        "a",
        "--owner",
        "a@example.com",
        "--scope",
        "read",
        "--expires",
        "1y",
    )
    assert code == 1
    assert "expires" in err


def test_scope_is_required(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as exc:
        main(
            [
                "--data-dir",
                str(tmp_path),
                "token",
                "create",
                "--name",
                "a",
                "--owner",
                "a@example.com",
            ]
        )
    assert exc.value.code == 2
