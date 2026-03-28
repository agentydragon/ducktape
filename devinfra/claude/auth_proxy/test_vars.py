"""Unit tests for auth_proxy/vars.py."""

import base64

import pytest_bazel

from devinfra.claude.auth_proxy.vars import normalize_proxy_url


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
