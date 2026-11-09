"""Curated secret rules shared by vault-mcp (reject on write) and vault-watcher (detect) (D24).

High-confidence patterns ported from gitleaks' default rules (MIT), without entropy rules.
There is deliberately no in-content allowlist: false positives are fixed by narrowing a rule
here, with a fixture test. Findings never carry the matched text, only a fingerprint.
"""

from __future__ import annotations

import difflib
import hashlib
import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Rule:
    id: str
    pattern: re.Pattern[str]
    group: str | None = None  # named group that holds the secret, when only part matches


@dataclass(frozen=True)
class Finding:
    rule: str
    line: int  # 1-based, in the scanned text
    fingerprint: str  # sha256(match)[:16]; dedupe key that never reveals the secret

    def masked(self) -> str:
        return f"{self.rule} at line {self.line}"


_PLACEHOLDER = re.compile(
    r"""^(?:
        <[^>]*> | \$\{[^}]*\} | \{\{[^}]*\}\} | \$[A-Z_][A-Z0-9_]* | %[A-Z_]+% |
        x+ | X+ | \*+ | \.{3,} |
        password | passwd | pass | pwd | secret | changeme | example | redacted
    )$""",
    re.VERBOSE | re.IGNORECASE,
)

RULES: tuple[Rule, ...] = (
    Rule("aws-access-key-id", re.compile(r"\b(?:A3T[A-Z0-9]|AKIA|ASIA|ABIA|ACCA)[A-Z2-7]{16}\b")),
    Rule("github-token", re.compile(r"\bgh[pousr]_[0-9A-Za-z]{36,255}\b")),
    Rule("github-fine-grained-pat", re.compile(r"\bgithub_pat_[0-9A-Za-z_]{82}\b")),
    Rule("gitlab-pat", re.compile(r"\bglpat-[0-9A-Za-z_-]{20}\b")),
    Rule("slack-token", re.compile(r"\bxox[abposr]-[0-9]{8,}-[0-9A-Za-z-]{16,}")),
    Rule(
        "slack-webhook",
        re.compile(
            r"https://hooks\.slack\.com/services/T[A-Z0-9]{6,}/B[A-Z0-9]{6,}/[A-Za-z0-9]{20,}"
        ),
    ),
    Rule("stripe-key", re.compile(r"\b(?:sk|rk)_(?:live|test|prod)_[0-9A-Za-z]{10,99}\b")),
    Rule("anthropic-api-key", re.compile(r"\bsk-ant-[a-z]+[0-9]*-[A-Za-z0-9_-]{32,}")),
    Rule(
        "openai-api-key",
        re.compile(
            r"\bsk-(?:proj|svcacct|admin)-[A-Za-z0-9_-]{40,}"
            r"|\bsk-[A-Za-z0-9]{20}T3BlbkFJ[A-Za-z0-9]{20}\b"
        ),
    ),
    Rule("gcp-api-key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}(?![0-9A-Za-z_-])")),
    Rule(
        "azure-storage-connection-string",
        re.compile(
            r"DefaultEndpointsProtocol=https?;AccountName=[^;\s]+;AccountKey=[A-Za-z0-9+/=]{40,}"
        ),
    ),
    Rule("private-key", re.compile(r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?-----")),
    Rule(
        "jwt",
        re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
    ),
    Rule(
        "connection-string-password",
        re.compile(r"\b[a-z][a-z0-9+.-]*://[^\s:/@]*:(?P<secret>[^\s/@]{3,})@[^\s/@]+"),
        group="secret",
    ),
    Rule(
        "generic-secret-assignment",
        re.compile(
            r"(?i)\b[a-z0-9_.-]*(?:password|passwd|pwd|secret|api[_-]?key|access[_-]?key|auth[_-]?token|token)\b"
            r"\s*[:=]\s*[\"']?(?P<secret>[^\s\"'#,;]{12,})"
        ),
        group="secret",
    ),
)

_GENERIC = "generic-secret-assignment"


def _plausible(rule: Rule, value: str) -> bool:
    if _PLACEHOLDER.match(value):
        return False
    if rule.id == "aws-access-key-id":
        return not value.endswith("EXAMPLE")  # AWS documentation keys (gitleaks allowlist)
    if rule.id == _GENERIC:
        return any(c.isdigit() for c in value) and any(c.isalpha() for c in value)
    return True


def _scan_line(line: str, lineno: int) -> list[Finding]:
    found: list[Finding] = []
    for rule in RULES:
        if rule.id == _GENERIC and found:
            continue  # a specific rule already explains this line
        for match in rule.pattern.finditer(line):
            value = match.group(rule.group) if rule.group else match.group(0)
            if _plausible(rule, value):
                digest = hashlib.sha256(match.group(0).encode()).hexdigest()[:16]
                found.append(Finding(rule.id, lineno, digest))
    return found


def scan_text(text: str) -> list[Finding]:
    findings: list[Finding] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        findings.extend(_scan_line(line, lineno))
    return findings


def scan_added_lines(old_text: str | None, new_text: str) -> list[Finding]:
    """D24 (MCP side): scan only lines that are new or changed relative to `old_text`, so a
    secret already sitting in a page doesn't block every later edit to it."""
    if old_text is None:
        return scan_text(new_text)
    old_lines, new_lines = old_text.splitlines(), new_text.splitlines()
    matcher = difflib.SequenceMatcher(None, old_lines, new_lines, autojunk=False)
    findings: list[Finding] = []
    for tag, _i1, _i2, j1, j2 in matcher.get_opcodes():
        if tag in ("replace", "insert"):
            for j in range(j1, j2):
                findings.extend(_scan_line(new_lines[j], j + 1))
    return findings
