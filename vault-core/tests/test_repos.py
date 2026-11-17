import pytest

from vault_core.repos import normalise_remote


@pytest.mark.parametrize(
    ("remote", "identity"),
    [
        ("git@github.com:Bookbot-Hive/Foo.git", "github.com/bookbot-hive/foo"),
        ("ssh://git@github.com/Bookbot-Hive/Foo.git", "github.com/bookbot-hive/foo"),
        ("https://github.com/Bookbot-Hive/Foo.git/", "github.com/bookbot-hive/foo"),
        ("github.com/Bookbot-Hive/Foo", "github.com/bookbot-hive/foo"),
        ("https://user:password@github.com/Org/Repo", "github.com/org/repo"),
        ("https://git.example:443/Group/Repo.git", "git.example/Group/Repo"),
        ("git@git.example:Group/Sub/Repo.git", "git.example/Group/Sub/Repo"),
        ("ssh://git@git.example:2222/Group/Repo.git", "git.example:2222/Group/Repo"),
        ("git.example:2222/Group/Repo", "git.example:2222/Group/Repo"),
        ("ssh://git@[::1]:2222/Group/Repo.git", "[::1]:2222/Group/Repo"),
    ],
)
def test_remote_identity_matches_equivalent_remote_forms(remote: str, identity: str) -> None:
    assert normalise_remote(remote) == identity


@pytest.mark.parametrize(
    "remote",
    [
        "",
        "/home/user/repo",
        "./repo",
        "../repo",
        "~/repo",
        "org/repo",
        "file:///tmp/repo",
        "https://github.com/org/repo/tree/main",
        "https://github.com/org/../repo",
        "https://github.com/org/repo?token=x",
        "https://github.com/org/repo#fragment",
        "https://github.com/org/re%00po",
        "https://github.com/org/re%20po",
        "https://github.com:bad/org/repo",
    ],
)
def test_local_unsafe_or_nonrepository_locations_have_no_remote_identity(remote: str) -> None:
    assert normalise_remote(remote) is None
