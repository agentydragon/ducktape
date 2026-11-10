"""Session cookie validation preserves the accepted wire character sets."""

import pytest
import pytest_bazel
from pydantic import TypeAdapter, ValidationError

from platform_sessions.http_types import CookieValue, HttpToken


@pytest.mark.parametrize("name", ["sessionKey", "x-forwarded-for", "!#$%&'*+.^_`|~09-Az"])
def test_http_tokens_accept_token_characters(name):
    assert TypeAdapter(HttpToken).validate_python(name) == name


@pytest.mark.parametrize("value", ["", " ", "bad;name", "bad:name", "bad\nname", "é"])
def test_http_tokens_reject_invalid_names(value):
    with pytest.raises(ValidationError):
        TypeAdapter(HttpToken).validate_python(value)


@pytest.mark.parametrize("value", ["session-123", "a=b", '"quoted"', r"back\slash"])
def test_cookie_values_preserve_existing_accepted_values(value):
    assert TypeAdapter(CookieValue).validate_python(value) == value


@pytest.mark.parametrize("value", ["", "bad value", "bad;value", "bad,value", "bad\nvalue", "é"])
def test_cookie_values_reject_delimiters_and_non_ascii(value):
    with pytest.raises(ValidationError):
        TypeAdapter(CookieValue).validate_python(value)


if __name__ == "__main__":
    pytest_bazel.main()
