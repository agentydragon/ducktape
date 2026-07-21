from __future__ import annotations

import httpx
import pytest
import pytest_bazel

from haku.console.oauth_token_support import OAuthTokenResponseError, parse_token_response, token_request_error_message


def test_token_request_timeout_has_a_message_when_httpx_error_does_not() -> None:
    request = httpx.Request("POST", "https://authorization.example/token")

    message = token_request_error_message(
        label="MCP OAuth token refresh", request_error=httpx.ReadTimeout("", request=request), timeout_seconds=10.0
    )

    assert message == "MCP OAuth token refresh timed out after 10 seconds"


def test_token_request_failure_preserves_error_class() -> None:
    request = httpx.Request("POST", "https://authorization.example/token")

    message = token_request_error_message(
        label="MCP OAuth token refresh",
        request_error=httpx.ReadError("connection reset", request=request),
        timeout_seconds=10.0,
    )

    assert message == "MCP OAuth token refresh request failed: ReadError: connection reset"


async def test_token_error_preserves_standard_oauth_details_without_tokens() -> None:
    response = httpx.Response(
        401,
        json={
            "error": "invalid_grant",
            "error_description": "refresh token was already rotated",
            "error_uri": "https://authorization.example/errors/invalid-grant",
            "access_token": "must-not-appear",
            "refresh_token": "must-not-appear-either",
        },
    )

    with pytest.raises(OAuthTokenResponseError) as exc_info:
        await parse_token_response(response, label="MCP OAuth token refresh")

    message = str(exc_info.value)
    assert message == (
        'MCP OAuth token refresh failed: 401: {"error":"invalid_grant",'
        '"error_description":"refresh token was already rotated",'
        '"error_uri":"https://authorization.example/errors/invalid-grant"}'
    )
    assert "must-not-appear" not in message
    assert exc_info.value.status_code == 401
    assert exc_info.value.oauth_error == "invalid_grant"
    assert not exc_info.value.invalid_response


async def test_token_error_preserves_bounded_plain_text_detail() -> None:
    response = httpx.Response(502, text="  upstream\nproxy timed out  " + "x" * 600)

    with pytest.raises(OAuthTokenResponseError) as exc_info:
        await parse_token_response(response, label="MCP OAuth token refresh")

    message = str(exc_info.value)
    assert message.startswith("MCP OAuth token refresh failed: 502: upstream proxy timed out ")
    assert len(message.removeprefix("MCP OAuth token refresh failed: 502: ")) == 512


async def test_token_error_does_not_dump_unknown_json_fields() -> None:
    response = httpx.Response(400, json={"detail": "contains internal data", "refresh_token": "secret"})

    with pytest.raises(OAuthTokenResponseError) as exc_info:
        await parse_token_response(response, label="MCP OAuth token refresh")

    assert str(exc_info.value) == (
        "MCP OAuth token refresh failed: 400: OAuth error response contained no standard error fields"
    )


if __name__ == "__main__":
    pytest_bazel.main()
