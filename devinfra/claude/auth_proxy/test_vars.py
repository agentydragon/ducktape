"""Unit tests for auth_proxy/vars.py."""

import base64

import pytest_bazel

from devinfra.claude.auth_proxy.vars import PROXY_ENV_VARS, get_proxy_url, normalize_proxy_url


def test_get_proxy_url_returns_first_match() -> None:
    """Returns the first PROXY_ENV_VARS entry found in the dict."""
    env = {"HTTPS_PROXY": "http://proxy1:8080", "https_proxy": "http://proxy2:8080"}
    assert get_proxy_url(env) == "http://proxy1:8080"


def test_get_proxy_url_falls_through_empty_values() -> None:
    """Skips empty values, returns the first non-empty match."""
    env = {"HTTPS_PROXY": "", "https_proxy": "http://proxy:8080"}
    assert get_proxy_url(env) == "http://proxy:8080"


def test_get_proxy_url_returns_none_when_absent() -> None:
    """Returns None when no proxy env var is set."""
    assert get_proxy_url({}) is None


def test_get_proxy_url_checks_all_vars() -> None:
    """Recognizes every variable in PROXY_ENV_VARS."""
    for var in PROXY_ENV_VARS:
        assert get_proxy_url({var: "http://proxy:1234"}) == "http://proxy:1234"


def test_normalize_proxy_url_with_credentials() -> None:
    """URL with embedded credentials: stripped from URL, placed in Proxy-Authorization."""
    url, headers = normalize_proxy_url("http://user:secret@proxy.example.com:8080")

    assert url == "http://proxy.example.com:8080"
    expected = "Basic " + base64.b64encode(b"user:secret").decode()
    assert headers == {"Proxy-Authorization": expected}


def test_normalize_proxy_url_no_credentials() -> None:
    """URL without credentials: returned unchanged with empty headers."""
    url, headers = normalize_proxy_url("http://proxy.example.com:8080")

    assert url == "http://proxy.example.com:8080"
    assert headers == {}


def test_normalize_proxy_url_port_preserved() -> None:
    """Port is retained in the sanitized URL."""
    url, _ = normalize_proxy_url("http://user:pass@proxy.example.com:15004")

    assert "15004" in url
    assert "user" not in url
    assert "pass" not in url


def test_normalize_proxy_url_special_chars_in_password() -> None:
    """Password with special characters is base64-encoded correctly."""
    url, headers = normalize_proxy_url("http://container_id:eyJhbGciOiJSUzI1NiJ9@egress:3128")

    assert url == "http://egress:3128"
    expected = "Basic " + base64.b64encode(b"container_id:eyJhbGciOiJSUzI1NiJ9").decode()
    assert headers == {"Proxy-Authorization": expected}


def test_normalize_proxy_url_empty_password() -> None:
    """Username with no password encodes correctly."""
    url, headers = normalize_proxy_url("http://user@proxy.example.com:8080")

    assert url == "http://proxy.example.com:8080"
    expected = "Basic " + base64.b64encode(b"user:").decode()
    assert headers == {"Proxy-Authorization": expected}


if __name__ == "__main__":
    pytest_bazel.main()
