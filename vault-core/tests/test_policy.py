from pathlib import Path

import pytest

from vault_core.policy import (
    Policy,
    PolicyError,
    check_read,
    check_write,
    load_policy,
)

SPEC_POLICY = """\
write:
  allow: [wiki/, raw/, _system/]
  human_pages: read_only
  delete: false
  max_file_mb: 10
read:
  deny: [private/, hr/]
rate_limits:
  requests_per_minute: 120
  writes_per_minute: 30
  writes_per_day: 2000
secrets:
  reject_on_write: true
"""


@pytest.fixture
def policy(tmp_path: Path) -> Policy:
    f = tmp_path / "vault-mcp.yaml"
    f.write_text(SPEC_POLICY)
    return load_policy(f)


def test_load_policy_reads_the_spec_example(policy: Policy) -> None:
    assert policy.write.allow == ["wiki/", "raw/", "_system/"]
    assert policy.read.deny == ["private/", "hr/"]
    assert policy.write.max_file_mb == 10
    assert policy.rate_limits.writes_per_day == 2000
    assert policy.secrets.reject_on_write is True


@pytest.mark.parametrize(
    ("yaml_text", "needle"),
    [
        (SPEC_POLICY.replace("max_file_mb: 10", "max_file_mb: -1"), "write.max_file_mb"),
        (SPEC_POLICY.replace("[wiki/, raw/, _system/]", "[wiki]"), "write.allow"),
        (SPEC_POLICY.replace("[private/, hr/]", "[../etc/]"), "read.deny"),
        (SPEC_POLICY.replace("delete: false", "delete: true"), "write.delete"),
        (SPEC_POLICY + "unknown_section: 1\n", "unknown_section"),
        ("write: [\n", "YAML"),
    ],
)
def test_invalid_policy_raises_error_naming_file_and_field(
    tmp_path: Path, yaml_text: str, needle: str
) -> None:
    f = tmp_path / "vault-mcp.yaml"
    f.write_text(yaml_text)
    with pytest.raises(PolicyError) as exc:
        load_policy(f)
    assert str(f) in str(exc.value)
    assert needle in str(exc.value)


def test_missing_policy_file_raises_policy_error(tmp_path: Path) -> None:
    with pytest.raises(PolicyError, match="not found"):
        load_policy(tmp_path / "nope.yaml")


# --- read rule (D8) -----------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "folders", "allowed", "reason"),
    [
        ("wiki/page.md", None, True, None),
        ("private/salaries.md", None, False, "denied"),
        ("hr/review.md", ["hr/"], False, "denied"),
        (".obsidian/app.json", None, False, "hidden"),
        ("projects/x.md", ["wiki/", "raw/"], False, "out_of_scope"),
        ("raw/source.md", ["wiki/", "raw/"], True, None),
        ("wikipedia/page.md", ["wiki/"], False, "out_of_scope"),
    ],
)
def test_check_read(
    policy: Policy, path: str, folders: list[str] | None, allowed: bool, reason: str | None
) -> None:
    decision = check_read(path, policy, folders)
    assert decision.allowed is allowed
    assert decision.reason == reason


# --- write rule (D9 truth table) ---------------------------------------------
# exists=False means a new file: there is no maintainer, only write.allow applies.


@pytest.mark.parametrize(
    ("path", "exists", "maintainer", "allowed", "reason"),
    [
        # inside write.allow
        ("wiki/page.md", False, None, True, None),
        ("wiki/page.md", True, None, True, None),
        ("wiki/page.md", True, "agent", True, None),
        ("wiki/page.md", True, "shared", True, None),
        ("wiki/page.md", True, "human", False, "human_page"),
        ("wiki/page.md", True, "someone-else", True, None),
        # outside write.allow
        ("projects/x.md", False, None, False, "not_in_write_allow"),
        ("projects/x.md", True, None, False, "not_in_write_allow"),
        ("projects/x.md", True, "agent", True, None),
        ("projects/x.md", True, "shared", True, None),
        ("projects/x.md", True, "human", False, "human_page"),
        ("projects/x.md", True, "someone-else", False, "not_in_write_allow"),
        # read rules apply first
        ("private/x.md", True, "agent", False, "denied"),
        (".obsidian/app.json", True, None, False, "hidden"),
    ],
)
def test_check_write_truth_table(
    policy: Policy,
    path: str,
    exists: bool,
    maintainer: str | None,
    allowed: bool,
    reason: str | None,
) -> None:
    decision = check_write(path, policy, folders=None, exists=exists, maintainer=maintainer)
    assert decision.allowed is allowed
    assert decision.reason == reason


def test_check_write_intersects_credential_folders_with_policy(policy: Policy) -> None:
    # agent-maintained page outside the credential's folders stays off-limits
    decision = check_write(
        "projects/x.md", policy, folders=["wiki/"], exists=True, maintainer="agent"
    )
    assert (decision.allowed, decision.reason) == (False, "out_of_scope")
    # folder scope cannot widen the policy: raw/ is allowed by scope but a human page wins
    decision = check_write("raw/x.md", policy, folders=["raw/"], exists=True, maintainer="human")
    assert (decision.allowed, decision.reason) == (False, "human_page")
