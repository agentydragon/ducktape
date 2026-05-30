"""Tests for ServerSettings (item registry loaded from a YAML config file)."""

from pathlib import Path
from textwrap import dedent

import pytest
import pytest_bazel

from plaid_utils.mcp_server.config import ServerSettings


def test_resolved_items_loads_yaml_and_resolves_tokens(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    items_yaml = tmp_path / "items.yaml"
    items_yaml.write_text(
        dedent("""
            items:
              - key: chase
                institution: Chase
                products: [transactions, liabilities]
                access_token_env: PLAID_CHASE_TOKEN
        """)
    )
    monkeypatch.setenv("PLAID_MCP_PLAID_ENV", "sandbox")
    monkeypatch.setenv("PLAID_MCP_CLIENT_ID", "cid")
    monkeypatch.setenv("PLAID_MCP_CLIENT_SECRET", "secret")
    monkeypatch.setenv("PLAID_MCP_ITEMS_CONFIG_PATH", str(items_yaml))
    monkeypatch.setenv("PLAID_CHASE_TOKEN", "tok-123")

    resolved = ServerSettings().resolved_items()

    assert set(resolved) == {"chase"}
    assert resolved["chase"].access_token == "tok-123"
    assert resolved["chase"].products == ["transactions", "liabilities"]


if __name__ == "__main__":
    pytest_bazel.main()
