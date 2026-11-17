"""`vault-mcp.yaml` policy model and the read/write rules (plan D8, D9, D27).

The rules are pure functions. The caller supplies whether the file exists and its
current `maintainer` (from the file on disk, never from incoming content).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PositiveFloat,
    PositiveInt,
    ValidationError,
    field_validator,
)

from vault_core.paths import PathError, is_hidden, normalize


class PolicyError(Exception):
    """The policy file is missing or invalid. The message names the file and the field."""


def validate_prefixes(prefixes: list[str]) -> list[str]:
    for prefix in prefixes:
        if not prefix.endswith("/"):
            raise ValueError(f"folder prefix must end with '/': {prefix!r}")
        try:
            normalize(prefix[:-1])
        except PathError as exc:
            raise ValueError(f"invalid folder prefix {prefix!r}: {exc}") from None
    return prefixes


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class WritePolicy(_Strict):
    allow: list[str]
    human_pages: Literal["read_only"] = "read_only"
    delete: Literal[False] = Field(False, description="No delete tool exists in v1 (D9).")
    max_file_mb: PositiveFloat = 10

    _check_allow = field_validator("allow")(validate_prefixes)


class ReadPolicy(_Strict):
    deny: list[str] = []

    _check_deny = field_validator("deny")(validate_prefixes)


class RateLimits(_Strict):
    requests_per_minute: PositiveInt = 120
    writes_per_minute: PositiveInt = 30
    writes_per_day: PositiveInt = 2000


class SecretsPolicy(_Strict):
    reject_on_write: bool = True


class Policy(_Strict):
    write: WritePolicy
    read: ReadPolicy = ReadPolicy()
    rate_limits: RateLimits = RateLimits()
    secrets: SecretsPolicy = SecretsPolicy()


def load_policy(path: Path) -> Policy:
    try:
        raw = yaml.safe_load(path.read_text())
    except FileNotFoundError:
        raise PolicyError(f"{path}: policy file not found") from None
    except yaml.YAMLError as exc:
        raise PolicyError(f"{path}: invalid YAML: {exc}") from None
    try:
        return Policy.model_validate(raw)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}" for err in exc.errors()
        )
        raise PolicyError(f"{path}: invalid policy: {problems}") from None


ReadReason = Literal["hidden", "denied", "out_of_scope"]
WriteReason = ReadReason | Literal["human_page", "not_in_write_allow"]


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: WriteReason | None = None


ALLOW = Decision(True)


def under(path: str, prefixes: list[str]) -> bool:
    return any(path.startswith(prefix) for prefix in prefixes)


def check_read(path: str, policy: Policy, folders: list[str] | None) -> Decision:
    """D8: hidden and `read.deny` paths are never served; credential folders narrow the rest."""
    if is_hidden(path):
        return Decision(False, "hidden")
    if under(path, policy.read.deny):
        return Decision(False, "denied")
    if folders is not None and not under(path, folders):
        return Decision(False, "out_of_scope")
    return ALLOW


def check_write(
    path: str,
    policy: Policy,
    folders: list[str] | None,
    *,
    exists: bool,
    maintainer: str | None,
) -> Decision:
    """D9: agent/shared pages are writable anywhere readable; human pages never; the rest only
    under `write.allow`. A new file has no maintainer, so only `write.allow` applies."""
    readable = check_read(path, policy, folders)
    if not readable.allowed:
        return readable
    current = maintainer if exists else None
    if current in ("agent", "shared"):
        return ALLOW
    if current == "human":
        return Decision(False, "human_page")
    if under(path, policy.write.allow):
        return ALLOW
    return Decision(False, "not_in_write_allow")
