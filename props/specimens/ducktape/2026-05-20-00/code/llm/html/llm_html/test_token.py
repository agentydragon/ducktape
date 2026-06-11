"""Tests for the TokenScheme verification logic.

Only the *verify_token* method is checked - the surrounding web server as well
as the CLI wrapper are intentionally excluded so the tests run fast and stay
deterministic.
"""

from datetime import datetime

import pytest
import pytest_bazel
from pydantic import BaseModel, Field

from llm.html.llm_html.server import TIMEZONE
from llm.html.llm_html.token_scheme import TokenScheme, VerificationError

SECRET = b"hunter2"


class TokenTestCase(BaseModel):
    """Model for token test configuration."""

    secret: bytes = Field(default=SECRET, description="Secret key for token generation")
    document: str = Field(default="test document", description="Document content")
    timestamp: datetime | None = Field(default=None, description="Token generation timestamp")


class TamperedTokenCase(BaseModel):
    """Model for tampered token test cases."""

    original: str = Field(description="Original valid token")
    position: int = Field(description="Position where tampering occurs")
    tampered: str = Field(description="Tampered token")
    expected_error: str = Field(description="Expected error message")


@pytest.fixture
def token_scheme():
    """Create a TokenScheme instance with a fixed secret and document."""
    test_case = TokenTestCase()
    return TokenScheme(test_case.secret, test_case.document)


@pytest.fixture
def fresh_valid_token(token_scheme):
    """Generate a fresh *valid* token and return the scheme instance & token."""
    prefix, bits = token_scheme.make_token(datetime.now())
    return prefix + "".join(bits)


def test_valid_token_verifies(token_scheme, fresh_valid_token):
    # Should *not* raise.
    token_scheme.verify_token(fresh_valid_token)


def test_doc_hash_mismatch_is_reported(token_scheme, fresh_valid_token):
    # Corrupt the very first character of the document hash part.
    # Layout: 1:MMDD-HH:MM-<doc><pub><priv>
    pos = fresh_valid_token.rfind("-") + 1
    tampered_char = "1" if fresh_valid_token[pos] != "1" else "2"

    tampered_case = TamperedTokenCase(
        original=fresh_valid_token,
        position=pos,
        tampered=fresh_valid_token[:pos] + tampered_char + fresh_valid_token[pos + 1 :],
        expected_error="Document hash mismatch",
    )

    with pytest.raises(VerificationError) as err:
        token_scheme.verify_token(tampered_case.tampered)

    assert any(tampered_case.expected_error in issue for issue in err.value.issues)


def test_incomplete_token_is_reported_but_does_not_crash(token_scheme, fresh_valid_token):
    # Strip the private hash so only doc+pub remain.
    incomplete_token = fresh_valid_token[: -token_scheme._AUTH_LEN]

    with pytest.raises(VerificationError) as err:
        token_scheme.verify_token(incomplete_token)

    assert any("Private hash incomplete" in issue for issue in err.value.issues)


def test_tokens_from_past_still_verify():
    """Ensure tokens issued at different times in the past are still valid."""
    past_case = TokenTestCase(timestamp=datetime(2000, 1, 1, 12, 0, 0, tzinfo=TIMEZONE))

    token_scheme = TokenScheme(past_case.secret, past_case.document)
    assert past_case.timestamp is not None  # We explicitly set it above
    prefix, bits = token_scheme.make_token(past_case.timestamp)
    past_token = prefix + "".join(bits)

    # Should verify successfully
    token_scheme.verify_token(past_token)


if __name__ == "__main__":
    pytest_bazel.main()
