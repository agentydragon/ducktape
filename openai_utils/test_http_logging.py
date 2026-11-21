"""The HTTP log keeps credentials redacted and the response body readable."""

import json
import logging
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
import pytest_bazel

from openai_utils.http_logging import make_logging_httpx_async_client


@pytest.fixture
async def log_client(tmp_path: Path) -> AsyncIterator[httpx.AsyncClient]:
    async with make_logging_httpx_async_client(tmp_path / "http.jsonl") as client:
        yield client


async def test_logs_masked_request_and_buffered_response(log_client: httpx.AsyncClient, tmp_path: Path) -> None:
    async def chunks() -> AsyncIterator[bytes]:
        yield b"\xffresponse "
        yield "café".encode()

    def respond(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(201, content=chunks(), headers={"x-result": "saved"})

    async with httpx.AsyncClient(event_hooks=log_client.event_hooks, transport=httpx.MockTransport(respond)) as client:
        response = await client.post(
            "https://example.test/v1/responses", headers={"Authorization": "Bearer secret"}, content=b"request\xff"
        )
        assert response.content == b"\xffresponse caf\xc3\xa9"
        assert await response.aread() == response.content

    log_path = tmp_path / "http.jsonl"
    request_record, response_record = [json.loads(line) for line in log_path.read_text().splitlines()]
    assert request_record["kind"] == "request"
    assert request_record["method"] == "POST"
    assert request_record["url"] == "https://example.test/v1/responses"
    assert request_record["headers"]["authorization"] == "***"
    assert request_record["body"] == "request"
    assert response_record == {
        "kind": "response",
        "status": 201,
        "req_url": request_record["url"],
        "req_headers": request_record["headers"],
        "req_body": "request",
        "resp_headers": dict(response.headers),
        "resp_body": "response café",
    }
    assert "Bearer secret" not in log_path.read_text()


class UnreadableStream(httpx.AsyncByteStream):
    def __init__(self, error: Exception) -> None:
        self.error = error

    async def __aiter__(self) -> AsyncIterator[bytes]:
        raise self.error
        yield b""  # pragma: no cover - declares an async generator


@pytest.mark.parametrize("error", [httpx.StreamConsumed(), httpx.StreamClosed(), httpx.ReadError("broken")])
async def test_unreadable_response_logs_empty_body(
    log_client: httpx.AsyncClient, tmp_path: Path, caplog: pytest.LogCaptureFixture, error: Exception
) -> None:
    caplog.set_level(logging.DEBUG)
    request = httpx.Request("GET", "https://example.test/v1/responses", headers={"Authorization": "Bearer secret"})
    response = httpx.Response(200, request=request, stream=UnreadableStream(error))
    try:
        for hook in log_client.event_hooks["response"]:
            await hook(response)
    finally:
        await response.aclose()

    assert any(
        record.name == "openai_utils.http_logging" and "failed to read response body:" in record.getMessage()
        for record in caplog.records
    )
    record = json.loads((tmp_path / "http.jsonl").read_text())
    assert record["resp_body"] == ""
    assert record["req_headers"]["authorization"] == "***"


if __name__ == "__main__":
    pytest_bazel.main()
