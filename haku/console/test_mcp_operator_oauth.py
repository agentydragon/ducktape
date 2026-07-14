"""Unit tests for the operator OAuth helpers."""

from __future__ import annotations

import pytest_bazel

from haku.console.mcp_operator_oauth import _oauth_callback_response


def test_callback_response_autoescapes_content_and_locks_down_browser_capabilities() -> None:
    hostile_message = '<img src=x onerror="alert(document.cookie)">'
    responses = [
        _oauth_callback_response(False, hostile_message, status_code=400),
        _oauth_callback_response(False, hostile_message, status_code=400),
    ]
    nonces: list[str] = []

    for response in responses:
        body = response.body.decode()
        csp = response.headers["Content-Security-Policy"]
        style_directive = csp.split("; ")[-1]
        nonce = style_directive.removeprefix("style-src 'nonce-").removesuffix("'")
        nonces.append(nonce)

        assert response.status_code == 400
        assert response.headers["Cache-Control"] == "no-store"
        assert response.headers["Referrer-Policy"] == "no-referrer"
        assert response.headers["X-Content-Type-Options"] == "nosniff"
        assert csp == (
            "default-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'; "
            f"script-src 'none'; style-src 'nonce-{nonce}'"
        )
        assert "unsafe-inline" not in csp
        assert f'<style nonce="{nonce}">' in body
        assert hostile_message not in body
        assert "&lt;img src=x onerror=&#34;alert(document.cookie)&#34;&gt;" in body

    assert nonces[0] != nonces[1]


if __name__ == "__main__":
    pytest_bazel.main()
