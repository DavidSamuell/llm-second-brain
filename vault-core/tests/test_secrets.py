"""Secret rules (D24). Fixtures are assembled at runtime so this file contains no literal
token that a repository secret scanner would flag."""

import hashlib

import pytest

from vault_core.secrets import RULES, Finding, scan_added_lines, scan_text

A = "A" * 40
D = "7" * 40


def j(*parts: str) -> str:
    return "".join(parts)


POSITIVES = {
    "aws-access-key-id": j("AKIA", "Q3RTZ7XK", "2P4LMNBV"),
    "github-token": j("gh", "p_", "a1B2" * 9),
    "github-fine-grained-pat": j("github_", "pat_", "11ABCDEFG0", "x" * 72),
    "gitlab-pat": j("gl", "pat-", "aB3dE5gH7jK9mN1pQ3sT"),
    "slack-token": j("xo", "xb-", "1234567890-1234567890123-", "aBcDeFgHiJkLmNoPqRsTuVwX"),
    "slack-webhook": j("https://hooks.", "slack.com/services/", "T0123ABCD/B0123ABCD/", "a1" * 12),
    "stripe-key": j("sk", "_live_", "51H", "aB3" * 10),
    "anthropic-api-key": j("sk-", "ant-", "api03-", "aB3_" * 20),
    "openai-api-key": j("sk-", "proj-", "aB3dE5gH7j" * 5),
    "gcp-api-key": j("AI", "za", "SyA-", "aB3dE5gH7jK9mN1pQ3sT5vW7yZ9bxyz"),
    "azure-storage-connection-string": j(
        "DefaultEndpointsProtocol=https;AccountName=acct;", "AccountKey=", "aB3+" * 22, "=="
    ),
    "private-key": j("-----BEGIN ", "RSA ", "PRIVATE KEY-----"),
    "jwt": j(
        "ey",
        "JhbGciOiJIUzI1NiJ9",
        ".",
        "ey",
        "JzdWIiOiIxMjM0NTY3ODkwIn0",
        ".",
        "dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U",
    ),
    "connection-string-password": j("postgres://app:", "Xk9#vR2pLq7z", "@db.internal:5432/main"),
    "generic-secret-assignment": j("api", "_key = ", '"', "q8Fz2LmX9vT4wR7k", '"'),
}


def test_every_rule_has_a_positive_fixture() -> None:
    assert {rule.id for rule in RULES} == set(POSITIVES)


@pytest.mark.parametrize(("rule_id", "secret"), sorted(POSITIVES.items()))
def test_rule_detects_its_fixture(rule_id: str, secret: str) -> None:
    findings = scan_text(f"# Notes\n\nsome text {secret} more text\n")
    assert [f.rule for f in findings] == [rule_id]
    assert findings[0].line == 3


NEGATIVES = [
    hashlib.sha256(b"page").hexdigest(),  # D7 version strings
    "version: " + hashlib.sha256(b"x").hexdigest(),
    "2f1c1c4e-6b9e-4a8f-9d6e-1a2b3c4d5e6f",  # uuid
    "The AKIA prefix marks AWS access keys.",
    "aws_access_key_id = " + j("AKIA", "IOSFODNN7", "EXAMPLE"),  # AWS documentation example key
    "Use postgres://user:password@localhost/db as a template.",
    "postgres://app:${DB_PASSWORD}@db:5432/main",
    "redis://:<password>@cache:6379",
    "password: required",
    "api_key = <your-key-here>",
    "token: ${GITHUB_TOKEN}",
    "secret = xxxxxxxxxxxxxxxx",
    "-----BEGIN PUBLIC KEY-----",
    "sk-ant is the prefix of Anthropic keys",
    "ghp_ tokens are classic GitHub tokens",
    "aGVsbG8gd29ybGQgdGhpcyBpcyBqdXN0IGJhc2U2NCB0ZXh0IG5vdCBhIHNlY3JldA==",
    "https://hooks.slack.com/services/T000/B000/XXXX",
]


@pytest.mark.parametrize("text", NEGATIVES)
def test_rules_ignore_lookalikes(text: str) -> None:
    assert scan_text(text) == []


def test_findings_never_carry_the_secret() -> None:
    secret = POSITIVES["stripe-key"]
    (finding,) = scan_text(secret)
    assert secret not in repr(finding)
    assert finding.fingerprint == hashlib.sha256(secret.encode()).hexdigest()[:16]
    assert finding.masked() == "stripe-key at line 1"


def test_scan_added_lines_ignores_secrets_already_present() -> None:
    old = f"# Page\n\nlegacy {POSITIVES['aws-access-key-id']}\n"
    new = old + "appended line\n"
    assert scan_added_lines(old, new) == []


def test_scan_added_lines_reports_new_and_changed_lines_with_new_line_numbers() -> None:
    old = "# Page\nline two\nline three\n"
    new = f"# Page\nline two {POSITIVES['github-token']}\nline three\nx {POSITIVES['jwt']}\n"
    findings = scan_added_lines(old, new)
    assert [(f.rule, f.line) for f in findings] == [("github-token", 2), ("jwt", 4)]


def test_scan_added_lines_for_new_file_scans_everything() -> None:
    findings = scan_added_lines(None, f"a\n{POSITIVES['private-key']}\n")
    assert findings == [Finding("private-key", 2, findings[0].fingerprint)]
