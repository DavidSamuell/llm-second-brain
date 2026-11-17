from pathlib import Path

import pytest

from vault_core.paths import PathError, is_hidden, normalize, resolve_in_vault


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "/etc/passwd",
        "../secret.md",
        "wiki/../../etc/passwd",
        "wiki/./page.md",
        "wiki//page.md",
        "wiki\\page.md",
        "wiki/pa\x00ge.md",
        "wiki/",
    ],
)
def test_normalize_rejects_non_canonical_or_escaping_paths(raw: str) -> None:
    with pytest.raises(PathError) as exc:
        normalize(raw)
    assert exc.value.code == "invalid"


def test_normalize_accepts_canonical_relative_path() -> None:
    assert normalize("wiki/concepts/Search Index.md") == "wiki/concepts/Search Index.md"


@pytest.mark.parametrize(
    ("path", "hidden"),
    [
        (".obsidian/app.json", True),
        (".git/config", True),
        (".trash/old.md", True),
        ("wiki/.draft.md", True),
        ("wiki/.vault-mcp-tmp/x.md", True),
        ("wiki/page.md", False),
        ("raw/_processing/src.md", False),
    ],
)
def test_is_hidden_flags_any_dot_segment(path: str, hidden: bool) -> None:
    assert is_hidden(path) is hidden


def test_resolve_in_vault_returns_path_inside_vault(tmp_path: Path) -> None:
    (tmp_path / "wiki").mkdir()
    assert resolve_in_vault(tmp_path, "wiki/page.md") == tmp_path.resolve() / "wiki" / "page.md"


def test_resolve_in_vault_rejects_symlink_escaping_the_vault(tmp_path: Path) -> None:
    vault, outside = tmp_path / "vault", tmp_path / "outside"
    vault.mkdir()
    outside.mkdir()
    (outside / "secret.md").write_text("x")
    (vault / "link").symlink_to(outside)
    with pytest.raises(PathError) as exc:
        resolve_in_vault(vault, "link/secret.md")
    assert exc.value.code == "forbidden"


@pytest.mark.parametrize("raw", ["alias/page.md", "wiki/page-alias.md", "alias/new.md"])
def test_resolve_in_vault_rejects_symlinks_even_inside_the_vault(tmp_path: Path, raw: str) -> None:
    """Policy is checked on the path string, so an alias like wiki/x.md -> ../private/y.md
    would otherwise inherit wiki/'s permissions. Tool paths never go through symlinks."""
    (tmp_path / "wiki").mkdir()
    (tmp_path / "wiki" / "page.md").write_text("x")
    (tmp_path / "alias").symlink_to(tmp_path / "wiki")
    (tmp_path / "wiki" / "page-alias.md").symlink_to(tmp_path / "wiki" / "page.md")
    with pytest.raises(PathError) as exc:
        resolve_in_vault(tmp_path, raw)
    assert exc.value.code == "forbidden"


def test_resolve_in_vault_accepts_a_vault_root_that_is_itself_a_symlink(tmp_path: Path) -> None:
    (tmp_path / "real" / "wiki").mkdir(parents=True)
    (tmp_path / "mounted").symlink_to(tmp_path / "real")
    assert resolve_in_vault(tmp_path / "mounted", "wiki/new.md") == (
        tmp_path / "real" / "wiki" / "new.md"
    )


def test_resolve_in_vault_rejects_hidden_paths(tmp_path: Path) -> None:
    with pytest.raises(PathError) as exc:
        resolve_in_vault(tmp_path, ".obsidian/app.json")
    assert exc.value.code == "forbidden"
