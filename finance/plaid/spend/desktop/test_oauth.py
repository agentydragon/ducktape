"""Tests for bounded and safe OAuth callback error reporting."""

from __future__ import annotations

import asyncio
from http import HTTPStatus
from typing import cast

import pytest_bazel

from finance.plaid.spend.desktop.oauth import Authenticator, CallbackResult, _callback_error, _format_callback_error


def test_callback_error_preserves_provider_description() -> None:
    callback = _callback_error(
        {"error": ["invalid_request"], "error_description": ["The request is otherwise malformed"]}
    )

    assert callback == CallbackResult(error="invalid_request", error_description="The request is otherwise malformed")
    assert callback is not None
    assert "authorization_code grant" in _format_callback_error(callback)


def test_callback_error_bounds_and_removes_control_characters() -> None:
    callback = _callback_error({"error": ["bad\x1b[31m_request"], "error_description": ["x" * 300]})

    assert callback is not None
    assert callback.error == "bad[31m_request"
    assert callback.error_description == "x" * 240


def test_callback_error_returns_none_without_oauth_error() -> None:
    assert _callback_error({"code": ["authorization-code"]}) is None


class _MemoryWriter:
    def __init__(self) -> None:
        self.parts: list[bytes] = []

    def write(self, value: bytes) -> None:
        self.parts.append(value)

    async def drain(self) -> None:
        pass


def test_callback_response_escapes_html() -> None:
    writer = _MemoryWriter()
    asyncio.run(
        Authenticator._write_callback_response(
            cast(asyncio.StreamWriter, writer), HTTPStatus.OK, "Rejected: <script>alert(1)</script> & retry"
        )
    )

    response = b"".join(writer.parts)
    body = response.split(b"\r\n\r\n", maxsplit=1)[1]
    assert b"&lt;script&gt;alert(1)&lt;/script&gt; &amp; retry" in body
    assert b"Content-Security-Policy:" in response


if __name__ == "__main__":
    pytest_bazel.main()
