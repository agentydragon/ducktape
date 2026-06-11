"""Test that rendered HTML contains all expected tokens."""

from unittest.mock import patch

import pytest
import pytest_bazel
from starlette.testclient import TestClient

from llm.html.llm_html.server import app
from llm.html.llm_html.token_scheme import N_TAGS, TokenScheme


@pytest.fixture
def mock_token_bits():
    """Create predictable token bits for testing."""
    return [chr(ord("a") + i) * 2 for i in range(N_TAGS)]


@pytest.fixture
def mock_token_scheme(mock_token_bits):
    """Mock TokenScheme.make_token to return predictable values."""
    with patch.object(TokenScheme, "make_token") as mock_make_token:
        mock_make_token.return_value = ("1:0123-01:23-", mock_token_bits)
        yield mock_make_token


@pytest.fixture
def client():
    """Create test client for FastAPI app."""
    return TestClient(app)


def test_each_tag_exactly_once(client, mock_token_scheme, mock_token_bits):
    """Test that rendered HTML contains all N tags (0 to N-1) exactly once."""
    response = client.get("/")
    assert response.status_code == 200

    html_content = response.text

    for i, expected_bit in enumerate(mock_token_bits):
        expected_tag = f"᚛{i}:{expected_bit}᚜"
        count = html_content.count(expected_tag)
        assert count == 1, f"Tag {expected_tag} found {count}x, not 1x"


if __name__ == "__main__":
    pytest_bazel.main()
