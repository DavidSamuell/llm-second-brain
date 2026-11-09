import hashlib
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from vault_mcp.store import CredentialError, CredentialStore, parse_expiry

T0 = datetime(2026, 11, 9, 12, 0, tzinfo=UTC)


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def store(tmp_path: Path, clock: Clock) -> CredentialStore:
    return CredentialStore(tmp_path / "tokens.db", now=clock)


def test_created_token_has_the_documented_format(store: CredentialStore) -> None:
    token, cred = store.create_bearer(name="alice-laptop", owner="alice@example.com", scope="read")
    assert re.fullmatch(r"vmcp_[A-Za-z0-9_-]{43}", token)
    assert cred.name == "alice-laptop"
    assert cred.owner == "alice@example.com"
    assert cred.kind == "bearer"
    assert cred.token_prefix == token[5:13]


def test_only_the_hash_is_stored(tmp_path: Path, store: CredentialStore) -> None:
    token, _ = store.create_bearer(name="a", owner="a@example.com", scope="write")
    raw = b"".join(p.read_bytes() for p in tmp_path.glob("tokens.db*"))
    assert token.encode() not in raw
    assert token[13:].encode() not in raw  # nothing beyond the 8-char display prefix
    assert hashlib.sha256(token.encode()).hexdigest().encode() in raw


def test_verify_returns_the_credential_with_its_persisted_attributes(
    store: CredentialStore,
) -> None:
    token, _ = store.create_bearer(
        name="ci", owner="ops@example.com", scope="write", folders=["wiki/", "raw/"], expires="30d"
    )
    cred = store.verify(token)
    assert cred is not None
    assert (cred.name, cred.owner, cred.scope, cred.folders) == (
        "ci",
        "ops@example.com",
        "write",
        ["wiki/", "raw/"],
    )
    assert cred.expires_at == T0 + timedelta(days=30)


@pytest.mark.parametrize("bad", ["", "vmcp_", "vmcp_" + "A" * 43, "Bearer x", "not-a-token"])
def test_unknown_tokens_do_not_verify(store: CredentialStore, bad: str) -> None:
    store.create_bearer(name="a", owner="a@example.com", scope="read")
    assert store.verify(bad) is None


def test_revoked_token_fails_verification_and_shows_as_revoked(store: CredentialStore) -> None:
    token, cred = store.create_bearer(name="a", owner="a@example.com", scope="read")
    store.revoke("a")
    assert store.verify(token) is None
    (listed,) = store.list_credentials()
    assert listed.id == cred.id
    assert listed.status(store.now()) == "revoked"


def test_revoke_by_id(store: CredentialStore) -> None:
    token, cred = store.create_bearer(name="a", owner="a@example.com", scope="read")
    store.revoke(cred.id)
    assert store.verify(token) is None


def test_revoking_an_unknown_name_is_an_error(store: CredentialStore) -> None:
    with pytest.raises(CredentialError, match="no active credential"):
        store.revoke("ghost")


def test_expired_token_fails_verification(store: CredentialStore, clock: Clock) -> None:
    token, _ = store.create_bearer(name="a", owner="a@example.com", scope="read", expires="1d")
    clock.now = T0 + timedelta(days=1, seconds=1)
    assert store.verify(token) is None
    assert store.list_credentials()[0].status(clock.now) == "expired"


def test_default_expiry_is_90_days_and_never_must_be_explicit(store: CredentialStore) -> None:
    _, default = store.create_bearer(name="a", owner="a@example.com", scope="read")
    _, forever = store.create_bearer(name="b", owner="b@example.com", scope="read", expires="never")
    assert default.expires_at == T0 + timedelta(days=90)
    assert forever.expires_at is None


@pytest.mark.parametrize(
    ("text", "delta"),
    [("90d", timedelta(days=90)), ("12h", timedelta(hours=12)), ("30m", timedelta(minutes=30))],
)
def test_parse_expiry(text: str, delta: timedelta) -> None:
    assert parse_expiry(text, T0) == T0 + delta


@pytest.mark.parametrize("text", ["", "0d", "-1d", "90", "1y", "forever", "None"])
def test_parse_expiry_rejects_other_values(text: str) -> None:
    with pytest.raises(CredentialError):
        parse_expiry(text, T0)


def test_active_names_are_unique_but_reusable_after_revoke(store: CredentialStore) -> None:
    store.create_bearer(name="a", owner="a@example.com", scope="read")
    with pytest.raises(CredentialError, match="already exists"):
        store.create_bearer(name="a", owner="other@example.com", scope="read")
    store.revoke("a")
    store.create_bearer(name="a", owner="a@example.com", scope="read")


@pytest.mark.parametrize(
    ("kwargs", "needle"),
    [
        ({"scope": "admin"}, "scope"),
        ({"folders": ["wiki"]}, "folder"),
        ({"folders": ["../etc/"]}, "folder"),
        ({"owner": "not-an-email"}, "owner"),
        ({"name": "has space"}, "name"),
    ],
)
def test_invalid_attributes_are_rejected(store: CredentialStore, kwargs: dict, needle: str) -> None:
    args = {"name": "a", "owner": "a@example.com", "scope": "read"} | kwargs
    with pytest.raises(CredentialError, match=needle):
        store.create_bearer(**args)


@pytest.mark.parametrize(
    ("scope", "can_read", "can_write"), [("read", True, False), ("write", True, True)]
)
def test_write_scope_implies_read(
    store: CredentialStore, scope: str, can_read: bool, can_write: bool
) -> None:
    _, cred = store.create_bearer(name="a", owner="a@example.com", scope=scope)
    assert (cred.allows("read"), cred.allows("write")) == (can_read, can_write)


def test_verify_records_last_use(store: CredentialStore, clock: Clock) -> None:
    token, _ = store.create_bearer(name="a", owner="a@example.com", scope="read")
    clock.now = T0 + timedelta(hours=1)
    store.verify(token)
    assert store.list_credentials()[0].last_used_at == T0 + timedelta(hours=1)


def test_database_file_is_private(tmp_path: Path, store: CredentialStore) -> None:
    store.create_bearer(name="a", owner="a@example.com", scope="read")
    assert (tmp_path / "tokens.db").stat().st_mode & 0o077 == 0
