"""Scaffold smoke test (T5): the package installs and imports.

Delete this file once the package has real behaviour tests.
"""

import importlib


def test_package_imports() -> None:
    assert importlib.import_module("vault_mcp").__name__ == "vault_mcp"
