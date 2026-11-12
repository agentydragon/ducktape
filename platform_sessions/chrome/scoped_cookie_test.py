from dataclasses import replace

import pytest
import pytest_bazel
from pydantic import ValidationError

from platform_sessions.chrome.profile import ChromeCookie
from platform_sessions.chrome.scoped_cookie import from_chrome
from platform_sessions.scoped_cookie import ScopedCookie


def chrome_cookie(**changes):
    return replace(
        ChromeCookie(
            host=".example.com",
            name="session",
            value="secret",
            encrypted_value=b"",
            path="/account",
            expires_utc=13300000000000000,
            secure=True,
            http_only=True,
            same_site=2,
            partition="",
            version=24,
        ),
        **changes,
    )


def test_chrome_cookie_preserves_scope_and_attributes():
    cookie = from_chrome(chrome_cookie(), ScopedCookie)
    assert cookie.model_dump() == {
        "name": "session",
        "value": "secret",
        "domain": ".example.com",
        "path": "/account",
        "secure": True,
        "httpOnly": True,
        "expires": 1655526400.0,
        "sameSite": "Strict",
    }
    assert "secret" not in repr(cookie)
    assert from_chrome(chrome_cookie(host="example.com", expires_utc=0), ScopedCookie).expires == -1


def test_partition_is_never_discarded():
    with pytest.raises(ValueError, match="Partitioned"):
        from_chrome(chrome_cookie(partition="https://example.com"), ScopedCookie)


@pytest.mark.parametrize("expiry", ["2026-01-02T03:04:05Z", "Fri, 02 Jan 2026 03:04:05 GMT"])
def test_har_cookie_preserves_scope_and_expiry(expiry):
    cookie = ScopedCookie.from_har(
        {"name": "session", "value": "secret", "domain": "example.com", "path": "/", "secure": True, "expires": expiry}
    )
    assert cookie.domain == "example.com"
    assert cookie.expires == 1767323045.0
    assert not cookie.httpOnly
    assert cookie.sameSite == "Lax"


def test_har_requires_explicit_scope_and_timezone():
    cookie = {"name": "session", "value": "secret", "domain": "example.com", "path": "/", "secure": True}
    assert ScopedCookie.from_har(cookie).expires == -1
    with pytest.raises(KeyError):
        ScopedCookie.from_har({key: value for key, value in cookie.items() if key != "domain"})
    with pytest.raises(ValueError, match="time zone"):
        ScopedCookie.from_har(cookie | {"expires": "2026-01-02T03:04:05"})


def test_schema_rejects_bad_scope_and_hides_credentials():
    data = from_chrome(chrome_cookie(), ScopedCookie).model_dump()
    for change in ({"domain": "bad\nhost"}, {"path": "relative"}, {"sameSite": "unknown"}):
        with pytest.raises(ValidationError) as error:
            ScopedCookie.model_validate(data | change)
        assert "secret" not in str(error.value)


if __name__ == "__main__":
    pytest_bazel.main()
