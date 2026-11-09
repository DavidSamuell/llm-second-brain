"""Fixtures for the end-to-end suite; the harness itself lives in harness.py."""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from harness import REPO, Stack, start, stop


@pytest.fixture(scope="session", autouse=True)
def _image() -> None:
    subprocess.run(["docker", "compose", "build", "vault-mcp"], cwd=REPO, check=True, env={
        **os.environ, "VAULT_PATH": "/nonexistent", "DATA_PATH": "/nonexistent",
    })  # fmt: skip


@pytest.fixture(
    scope="session",
    params=[
        pytest.param("local", marks=pytest.mark.profile_local),
        pytest.param("none", marks=pytest.mark.profile_none),
    ],
)
def stack(request: pytest.FixtureRequest) -> Iterator[Stack]:
    s = start(request.param)
    yield s
    stop(s)
