"""Tests for claude_hooks.proxy_credentials module."""

from __future__ import annotations

import time
from datetime import UTC, datetime

import jwt
import pytest
import pytest_bazel

from tools.claude_hooks.proxy_credentials import (
    CredentialStatus,
    build_upstream_uri,
    check_credential_expiry,
    get_jwt_expiry,
    parse_proxy_url,
)


class TestParseProxyUrl:
    """Tests for parse_proxy_url()."""

    def test_simple_url(self) -> None:
        result = parse_proxy_url("http://proxy.example.com:8080")
        assert result.hostname == "proxy.example.com"
        assert result.port == 8080
        assert result.username is None
        assert result.password is None

    def test_url_with_credentials(self) -> None:
        result = parse_proxy_url("http://user:pass@proxy.example.com:8080")
        assert result.hostname == "proxy.example.com"
        assert result.port == 8080
        assert result.username == "user"
        assert result.password == "pass"

    def test_url_with_complex_password(self) -> None:
        # JWT tokens contain special chars
        result = parse_proxy_url("http://container:jwt_eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ0ZXN0In0.abc@proxy:15004")
        assert result.hostname == "proxy"
        assert result.port == 15004
        assert result.username == "container"
        assert result.password == "jwt_eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ0ZXN0In0.abc"

    def test_invalid_url_raises(self) -> None:
        with pytest.raises(ValueError, match="Could not parse host"):
            parse_proxy_url("not-a-url")


class TestBuildUpstreamUri:
    """Tests for build_upstream_uri()."""

    def test_no_credentials(self) -> None:
        proxy = parse_proxy_url("http://proxy.example.com:8080")
        uri = build_upstream_uri(proxy)
        assert uri == "http://proxy.example.com:8080"
        assert "#" not in uri

    def test_with_credentials(self) -> None:
        proxy = parse_proxy_url("http://user:pass@proxy.example.com:8080")
        uri = build_upstream_uri(proxy)
        # Legacy format: http://host:port#user:pass
        assert uri == "http://proxy.example.com:8080#user:pass"

    def test_username_only(self) -> None:
        proxy = parse_proxy_url("http://user@proxy.example.com:8080")
        uri = build_upstream_uri(proxy)
        assert uri == "http://proxy.example.com:8080#user:"

    def test_default_port(self) -> None:
        proxy = parse_proxy_url("http://proxy.example.com")
        uri = build_upstream_uri(proxy)
        assert uri == "http://proxy.example.com:80"


class TestGetJwtExpiry:
    """Tests for get_jwt_expiry()."""

    def test_valid_jwt_with_exp(self) -> None:
        # Create a JWT with exp claim
        exp_time = int(time.time()) + 3600  # 1 hour from now
        token = jwt.encode({"exp": exp_time, "sub": "test"}, "secret", algorithm="HS256")
        result = get_jwt_expiry(token)
        assert result is not None
        assert abs((result - datetime.fromtimestamp(exp_time, tz=UTC)).total_seconds()) < 1

    def test_valid_jwt_without_exp(self) -> None:
        token = jwt.encode({"sub": "test"}, "secret", algorithm="HS256")
        result = get_jwt_expiry(token)
        assert result is None

    def test_invalid_jwt_returns_none(self) -> None:
        assert get_jwt_expiry("not-a-jwt") is None
        assert get_jwt_expiry("also.not.valid") is None
        assert get_jwt_expiry("") is None

    def test_jwt_with_future_exp(self) -> None:
        exp_time = int(time.time()) + 7200  # 2 hours from now
        token = jwt.encode({"exp": exp_time}, "secret", algorithm="HS256")
        result = get_jwt_expiry(token)
        assert result is not None
        assert result > datetime.now(UTC)


class TestCredentialStatus:
    """Tests for CredentialStatus dataclass."""

    def test_stores_expiry(self) -> None:
        expiry = datetime.fromtimestamp(time.time() + 15 * 60, tz=UTC)
        status = CredentialStatus(expiry=expiry)
        assert status.expiry == expiry

    def test_stores_none_expiry(self) -> None:
        status = CredentialStatus(expiry=None)
        assert status.expiry is None


class TestCheckCredentialExpiry:
    """Tests for check_credential_expiry()."""

    def test_passwordless_url(self) -> None:
        status = check_credential_expiry("http://proxy:8080")
        assert status.expiry is None

    def test_non_jwt_password(self) -> None:
        status = check_credential_expiry("http://user:simplepass@proxy:8080")
        assert status.expiry is None

    def test_valid_jwt(self) -> None:
        exp_time = int(time.time()) + 7200  # 2 hours from now
        token = jwt.encode({"exp": exp_time}, "secret", algorithm="HS256")
        status = check_credential_expiry(f"http://user:{token}@proxy:8080")
        assert status.expiry is not None
        assert status.expiry > datetime.now(UTC)

    def test_expired_jwt(self) -> None:
        exp_time = int(time.time()) - 3600  # 1 hour ago
        token = jwt.encode({"exp": exp_time}, "secret", algorithm="HS256")
        status = check_credential_expiry(f"http://user:{token}@proxy:8080")
        assert status.expiry is not None
        assert status.expiry < datetime.now(UTC)

    def test_jwt_expiring_soon(self) -> None:
        exp_time = int(time.time()) + 120  # 2 minutes from now
        token = jwt.encode({"exp": exp_time}, "secret", algorithm="HS256")
        status = check_credential_expiry(f"http://user:{token}@proxy:8080")
        assert status.expiry is not None
        # Expiry is in future but close
        assert status.expiry > datetime.now(UTC)
        minutes_remaining = (status.expiry - datetime.now(UTC)).total_seconds() / 60
        assert minutes_remaining < 30

    def test_jwt_with_prefix(self) -> None:
        exp_time = int(time.time()) + 7200
        token = jwt.encode({"exp": exp_time}, "secret", algorithm="HS256")
        status = check_credential_expiry(f"http://user:jwt_{token}@proxy:8080")
        assert status.expiry is not None


if __name__ == "__main__":
    pytest_bazel.main()
