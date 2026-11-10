"""ignis-init (SPEC §8, §16.17, plan D28)."""

import hashlib
import json
from pathlib import Path

import pytest
import yaml

from vault_watcher.ignis_harden import (
    HardenError,
    check_data_mounts,
    compose_override,
    harden,
    load_allowlist,
)

MAIN, MANIFEST = b"module.exports = class P {};\n", b'{"id": "good-plugin", "version": "1.0.0"}\n'
TEMPLATE = Path(__file__).resolve().parents[2] / "vault-template"


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class Releases:
    """Fake GitHub release downloads, recording every URL fetched."""

    def __init__(self) -> None:
        self.files = {
            "https://github.com/acme/good-plugin/releases/download/1.0.0/main.js": MAIN,
            "https://github.com/acme/good-plugin/releases/download/1.0.0/manifest.json": MANIFEST,
        }
        self.fetched: list[str] = []

    def __call__(self, url: str) -> bytes:
        self.fetched.append(url)
        return self.files[url]


def allowlist(tmp_path: Path, *, main_sha: str | None = None, writable: bool = False) -> Path:
    path = tmp_path / "ignis-plugins.yaml"
    entry = {"id": "good-plugin", "repo": "acme/good-plugin", "version": "1.0.0",
             "sha256": {"main.js": main_sha or sha(MAIN), "manifest.json": sha(MANIFEST)}, "writable_data": writable}  # fmt: skip
    path.write_text(yaml.safe_dump({"plugins": [entry]}))
    return path


@pytest.fixture
def dirs(tmp_path: Path) -> tuple[Path, Path]:
    vault, data = tmp_path / "vault", tmp_path / "data"
    vault.mkdir()
    data.mkdir()
    return vault, data


def run(
    tmp_path: Path, vault: Path, data: Path, allow: Path, get: Releases, mounted: str = ""
) -> list[str]:
    return harden(allowlist=allow, vault_dir=vault, data_dir=data, template_dir=TEMPLATE,
                  base_url="https://github.com", mounted=mounted, get=get)  # fmt: skip


def test_16_17_unlisted_plugins_are_removed_and_allowlisted_ones_installed(
    tmp_path: Path, dirs: tuple[Path, Path]
) -> None:
    vault, data = dirs
    rogue = data / "ignis-plugins" / "rogue-plugin"
    rogue.mkdir(parents=True)
    (rogue / "main.js").write_text("evil()")
    get = Releases()
    removed = run(tmp_path, vault, data, allowlist(tmp_path), get)
    assert removed == ["rogue-plugin"]
    plugin = data / "ignis-plugins" / "good-plugin"
    assert (plugin / "main.js").read_bytes() == MAIN
    assert sorted(p.name for p in (data / "ignis-plugins").iterdir()) == ["good-plugin"]
    assert json.loads((vault / ".obsidian" / "community-plugins.json").read_text()) == [
        "good-plugin"
    ]
    assert (vault / ".obsidian" / "plugins").is_dir()  # the mount point exists before Ignis starts


def test_verified_files_are_not_downloaded_again_and_tampered_ones_are_replaced(
    tmp_path: Path, dirs: tuple[Path, Path]
) -> None:
    vault, data = dirs
    allow, get = allowlist(tmp_path), Releases()
    run(tmp_path, vault, data, allow, get)
    assert len(get.fetched) == 2
    (data / "ignis-plugins" / "good-plugin" / "main.js").write_bytes(b"tampered")
    (data / "ignis-plugins" / "good-plugin" / "extra.js").write_bytes(b"dropped in")
    run(tmp_path, vault, data, allow, get)
    assert len(get.fetched) == 3  # only main.js again
    assert (data / "ignis-plugins" / "good-plugin" / "main.js").read_bytes() == MAIN
    assert not (data / "ignis-plugins" / "good-plugin" / "extra.js").exists()


def test_a_checksum_mismatch_aborts_before_writing_anything(
    tmp_path: Path, dirs: tuple[Path, Path]
) -> None:
    vault, data = dirs
    with pytest.raises(HardenError, match="checksum mismatch"):
        run(tmp_path, vault, data, allowlist(tmp_path, main_sha="0" * 64), Releases())
    assert not (data / "ignis-plugins" / "good-plugin" / "main.js").exists()


def test_managed_settings_are_reset_from_the_template(
    tmp_path: Path, dirs: tuple[Path, Path]
) -> None:
    vault, data = dirs
    (vault / ".obsidian").mkdir()
    (vault / ".obsidian" / "app.json").write_text('{"attachmentFolderPath": "/"}')
    (vault / ".obsidian" / "hotkeys.json").write_text('{"mine": 1}')
    run(tmp_path, vault, data, allowlist(tmp_path), Releases())
    assert (vault / ".obsidian" / "app.json").read_bytes() == (
        TEMPLATE / ".obsidian" / "app.json"
    ).read_bytes()
    assert (vault / ".obsidian" / "hotkeys.json").read_text() == '{"mine": 1}'  # not kit-managed


def test_an_empty_allowlist_removes_every_plugin(tmp_path: Path, dirs: tuple[Path, Path]) -> None:
    vault, data = dirs
    (data / "ignis-plugins" / "x").mkdir(parents=True)
    empty = tmp_path / "empty.yaml"
    empty.write_text("plugins: []\n")
    run(tmp_path, vault, data, empty, Releases())
    assert list((data / "ignis-plugins").iterdir()) == []
    assert json.loads((vault / ".obsidian" / "community-plugins.json").read_text()) == []


def test_writable_plugins_need_their_data_mount(tmp_path: Path, dirs: tuple[Path, Path]) -> None:
    vault, data = dirs
    allow = allowlist(tmp_path, writable=True)
    with pytest.raises(HardenError, match=r"regenerate compose\.plugins\.yml"):
        run(tmp_path, vault, data, allow, Releases())
    run(tmp_path, vault, data, allow, Releases(), mounted="good-plugin")
    assert (data / "ignis-plugins" / "good-plugin" / "data.json").exists()  # mount target
    assert (
        data / "ignis-plugin-data" / "good-plugin" / "data.json"
    ).read_text() == "{}\n"  # mount source
    override = yaml.safe_load(compose_override(load_allowlist(allow)))
    assert (
        override["services"]["ignis-init"]["environment"]["IGNIS_PLUGIN_DATA_MOUNTS"]
        == "good-plugin"
    )
    assert override["services"]["ignis"]["volumes"] == [
        "${DATA_PATH}/ignis-plugin-data/good-plugin/data.json:/vaults/${VAULT_NAME:-Docs}/.obsidian/plugins/good-plugin/data.json"
    ]
    check_data_mounts(load_allowlist(allow), "good-plugin")


@pytest.mark.parametrize(
    "entry",
    [
        {
            "id": "../escape",
            "repo": "a/b",
            "version": "1",
            "sha256": {"main.js": "a" * 64, "manifest.json": "a" * 64},
        },
        {
            "id": "ok",
            "repo": "not a repo",
            "version": "1",
            "sha256": {"main.js": "a" * 64, "manifest.json": "a" * 64},
        },
        {
            "id": "ok",
            "repo": "a/b",
            "version": "../1",
            "sha256": {"main.js": "a" * 64, "manifest.json": "a" * 64},
        },
        {"id": "ok", "repo": "a/b", "version": "1", "sha256": {"main.js": "a" * 64}},
        {
            "id": "ok",
            "repo": "a/b",
            "version": "1",
            "sha256": {"main.js": "a" * 64, "manifest.json": "a" * 64, "evil.sh": "a" * 64},
        },
        {
            "id": "ok",
            "repo": "a/b",
            "version": "1",
            "sha256": {"main.js": "XYZ", "manifest.json": "a" * 64},
        },
    ],
)
def test_invalid_allowlist_entries_are_rejected(tmp_path: Path, entry: dict[str, object]) -> None:
    path = tmp_path / "bad.yaml"
    path.write_text(yaml.safe_dump({"plugins": [entry]}))
    with pytest.raises(HardenError):
        load_allowlist(path)


def test_the_shipped_allowlist_is_valid() -> None:
    load_allowlist(Path(__file__).resolve().parents[2] / "ignis" / "ignis-plugins.yaml")
