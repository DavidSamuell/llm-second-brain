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


# --- v0.2 write-policy (SPEC-write-policy.md, D40) ----------------------------

V2_POLICY = """\
write:
  allow: [wiki/, raw/, _system/, projects/, specs/, pipelines/, runbooks/]
  human_pages: edit_with_notice
  protected: [AGENTS.md, CLAUDE.md, skills/, _system/templates/, _system/graph/ontology.yaml]
  human_edit_grace_s: 90
  delete: false
  max_file_mb: 10
read:
  deny: [private/, hr/]
"""


@pytest.fixture
def policy_v2(tmp_path: Path) -> Policy:
    f = tmp_path / "vault-mcp.yaml"
    f.write_text(V2_POLICY)
    return load_policy(f)


def test_shipped_example_policy_matches_the_spec_defaults(policy_v2: Policy) -> None:
    example = Path(__file__).resolve().parents[2] / "vault-mcp.example.yaml"
    assert load_policy(example).write == policy_v2.write


def test_v01_policy_file_still_loads_with_safe_v02_defaults(policy: Policy) -> None:
    # an existing deployment's file has no protected/grace keys: rule files are still protected
    assert policy.write.human_pages == "read_only"
    assert policy.write.human_edit_grace_s == 90
    assert "AGENTS.md" in policy.write.protected
    assert "skills/" in policy.write.protected


@pytest.mark.parametrize(
    ("path", "exists", "maintainer", "saved_ago", "allowed", "reason", "human_page"),
    [
        # human-facing folders are writable now
        ("projects/alpha.md", False, None, None, True, None, False),
        ("pipelines/etl.md", True, None, None, True, None, False),
        ("specs/alpha/arch.md", True, "shared", None, True, None, False),
        # person-maintained pages: allowed, flagged for the notice
        ("projects/alpha.md", True, "human", None, True, None, True),
        ("wiki/x.md", True, "human", 3600.0, True, None, True),
        # protected files and folders, whatever the frontmatter says
        ("AGENTS.md", True, "human", None, False, "protected", False),
        ("CLAUDE.md", True, "shared", None, False, "protected", False),
        ("skills/docs-ingest/SKILL.md", True, "agent", None, False, "protected", False),
        ("_system/templates/runbook.md", True, None, None, False, "protected", False),
        ("_system/graph/ontology.yaml", True, None, None, False, "protected", False),
        ("skills-notes.md", False, None, None, False, "not_in_write_allow", False),
        # a fresh web save waits, for every kind of page
        ("projects/alpha.md", True, "shared", 10.0, False, "recently_edited", False),
        ("wiki/x.md", True, "agent", 89.9, False, "recently_edited", False),
        ("wiki/x.md", True, "agent", 90.0, True, None, False),
        # read rules still come first
        ("private/x.md", True, "agent", 5.0, False, "denied", False),
        ("hr/AGENTS.md", True, None, None, False, "denied", False),
    ],
)
def test_check_write_v2_decision_order(
    policy_v2: Policy,
    path: str,
    exists: bool,
    maintainer: str | None,
    saved_ago: float | None,
    allowed: bool,
    reason: str | None,
    human_page: bool,
) -> None:
    decision = check_write(
        path, policy_v2, folders=None, exists=exists, maintainer=maintainer,
        web_saved_s_ago=saved_ago,
    )  # fmt: skip
    assert (decision.allowed, decision.reason, decision.human_page) == (allowed, reason, human_page)


def test_read_only_mode_still_refuses_human_pages_and_protects_rule_files(policy: Policy) -> None:
    human = check_write("raw/x.md", policy, folders=None, exists=True, maintainer="human")
    assert (human.allowed, human.reason) == (False, "human_page")
    rules = check_write("AGENTS.md", policy, folders=None, exists=True, maintainer="agent")
    assert (rules.allowed, rules.reason) == (False, "protected")


@pytest.mark.parametrize(
    ("yaml_text", "needle"),
    [
        (
            V2_POLICY.replace("human_edit_grace_s: 90", "human_edit_grace_s: -1"),
            "human_edit_grace_s",
        ),
        (V2_POLICY.replace("edit_with_notice", "anything_goes"), "human_pages"),
        (V2_POLICY.replace("[AGENTS.md,", "[../AGENTS.md,"), "write.protected"),
    ],
)
def test_invalid_v2_fields_name_the_field(tmp_path: Path, yaml_text: str, needle: str) -> None:
    f = tmp_path / "vault-mcp.yaml"
    f.write_text(yaml_text)
    with pytest.raises(PolicyError, match=needle):
        load_policy(f)
