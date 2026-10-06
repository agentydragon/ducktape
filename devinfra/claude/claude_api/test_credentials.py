"""Claude Code's `.credentials.json`: its camelCase keys, and `None` when the file cannot be read."""

import json
from pathlib import Path

import pytest
import pytest_bazel

from devinfra.claude.claude_api.credentials import read_credentials


def test_read_credentials(patch_credentials_path: Path):
    patch_credentials_path.write_text(
        json.dumps({"claudeAiOauth": {"accessToken": "test-token-123", "subscriptionType": "max"}})
    )

    oauth = read_credentials()
    assert oauth is not None
    assert oauth.access_token == "test-token-123"
    assert oauth.subscription_type == "max"


@pytest.mark.usefixtures("no_credentials")
def test_read_credentials_missing_file():
    assert read_credentials() is None


def test_read_credentials_malformed(patch_credentials_path: Path):
    patch_credentials_path.write_text("not json")

    assert read_credentials() is None


if __name__ == "__main__":
    pytest_bazel.main()
