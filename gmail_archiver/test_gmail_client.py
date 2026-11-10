"""Tests for batched Gmail message retrieval without a live Gmail API."""

import base64
import json
import re
from dataclasses import dataclass
from typing import Any, cast
from urllib.parse import unquote, urlsplit

from googleapiclient.http import BatchHttpRequest, HttpMock, HttpRequest
from googleapiclient.model import JsonModel

from gmail_archiver.gmail_client import GmailClient
from gmail_archiver.models import Email

_BATCH_URI = "https://gmail.googleapis.com/batch/gmail/v1"
_MESSAGE_PREFIX = "https://gmail.googleapis.com/gmail/v1/users/me/messages/"
_RAW_BYTES = b"From: sender@example.com\r\nSubject: test\r\n\r\nbody"


@dataclass
class _Outcome:
    status: int
    response: dict[str, object]
    retry_after: str | None = None


class _MockTransport:
    """Mock transport that runs real googleapiclient BatchHttpRequest parsing."""

    def __init__(
        self, batch_responses: list[dict[str, _Outcome]], individual_responses: dict[str, list[_Outcome]] | None = None
    ) -> None:
        self.batch_responses = list(batch_responses)
        self.individual_responses = individual_responses or {}
        self.batch_calls: list[list[str]] = []
        self.individual_calls: list[str] = []

    def request(
        self,
        uri: str,
        method: str = "GET",
        body: str | bytes | None = None,
        headers: dict[str, str] | None = None,
        redirections: int = 1,
        connection_type: object | None = None,
    ) -> tuple[object, bytes]:
        if uri == _BATCH_URI:
            assert body is not None
            request_body = body.decode() if isinstance(body, bytes) else body
            request_headers = headers or {}
            boundary = re.search(r'boundary="?([^";]+)', request_headers["content-type"])
            assert boundary is not None

            request_parts: list[tuple[str, str]] = []
            for part in request_body.split(f"--{boundary.group(1)}"):
                if "Content-Type: application/http" not in part:
                    continue
                content_id = re.search(r"Content-ID:\s*<([^>]+)>", part)
                request_line = re.search(r"\bGET\s+(\S+)\s+HTTP/1\.1", part)
                assert content_id is not None
                assert request_line is not None
                message_id = unquote(urlsplit(request_line.group(1)).path.rsplit("/", 1)[1])
                request_parts.append((content_id.group(1), message_id))

            message_ids = [message_id for _, message_id in request_parts]
            self.batch_calls.append(message_ids)
            outcomes = self.batch_responses.pop(0)
            assert set(outcomes) == set(message_ids)
            response_boundary = "gmail-test-response"
            response_parts = []
            for request_id, message_id in request_parts:
                response_parts.append(self._multipart_part(request_id, outcomes[message_id]))
            response_body = "".join(response_parts) + f"--{response_boundary}--\r\n"
            return self._sdk_response(
                uri,
                method,
                body,
                headers,
                response_body.encode(),
                {"status": "200", "content-type": f"multipart/mixed; boundary={response_boundary}"},
            )

        message_id = unquote(urlsplit(uri).path.rsplit("/", 1)[1])
        self.individual_calls.append(message_id)
        outcome = self.individual_responses[message_id].pop(0)
        return self._sdk_response(
            uri, method, body, headers, json.dumps(outcome.response).encode(), self._headers(outcome)
        )

    @staticmethod
    def _headers(outcome: _Outcome) -> dict[str, str]:
        headers = {"status": str(outcome.status), "content-type": "application/json"}
        if outcome.retry_after is not None:
            headers["retry-after"] = outcome.retry_after
        return headers

    @classmethod
    def _multipart_part(cls, content_id: str, outcome: _Outcome) -> str:
        reason = {200: "OK", 403: "Forbidden", 404: "Not Found", 429: "Too Many Requests", 503: "Unavailable"}.get(
            outcome.status, "Error"
        )
        headers = cls._headers(outcome)
        nested_headers = "".join(
            f"{name}: {value}\r\n" for name, value in headers.items() if name not in ("status", "content-type")
        )
        return (
            "--gmail-test-response\r\n"
            "Content-Type: application/http\r\n"
            f"Content-ID: <{content_id}>\r\n\r\n"
            f"HTTP/1.1 {outcome.status} {reason}\r\n"
            "Content-Type: application/json; charset=UTF-8\r\n"
            f"{nested_headers}\r\n"
            f"{json.dumps(outcome.response)}\r\n"
        )

    @staticmethod
    def _sdk_response(
        uri: str,
        method: str,
        request_body: str | bytes | None,
        request_headers: dict[str, str] | None,
        response_body: bytes,
        response_headers: dict[str, str],
    ) -> tuple[Any, bytes]:
        response = HttpMock(headers=response_headers)
        response.data = response_body
        return cast(tuple[Any, bytes], response.request(uri, method, request_body, request_headers))


class _MockMessages:
    def __init__(self, transport: _MockTransport) -> None:
        self.transport = transport

    def get(self, *, userId: str, id: str, format: str) -> HttpRequest:  # noqa: N803 - matches Gmail API parameter
        return cast(
            HttpRequest,
            HttpRequest(
                http=self.transport,
                postproc=JsonModel().response,
                uri=f"{_MESSAGE_PREFIX}{id}?format={format}",
                method="GET",
            ),
        )


class _MockUsers:
    def __init__(self, transport: _MockTransport) -> None:
        self.transport = transport

    def messages(self) -> _MockMessages:
        return _MockMessages(self.transport)


class _MockGmailService:
    def __init__(self, transport: _MockTransport) -> None:
        self.transport = transport

    def users(self) -> _MockUsers:
        return _MockUsers(self.transport)

    def new_batch_http_request(self) -> BatchHttpRequest:
        return cast(BatchHttpRequest, BatchHttpRequest(batch_uri=_BATCH_URI))


def _raw_message(message_id: str) -> dict[str, object]:
    return {
        "id": message_id,
        "threadId": f"thread-{message_id}",
        "labelIds": ["INBOX"],
        "internalDate": "1700000000000",
        "snippet": "test message",
        "raw": base64.urlsafe_b64encode(_RAW_BYTES).decode(),
    }


def _metadata_message(message_id: str) -> dict[str, object]:
    return {
        "id": message_id,
        "threadId": f"thread-{message_id}",
        "labelIds": ["INBOX"],
        "internalDate": "1700000000000",
        "payload": {"headers": [{"name": "Subject", "value": f"Subject {message_id}"}]},
    }


def _ok(message_id: str, *, metadata: bool = False) -> _Outcome:
    return _Outcome(200, _metadata_message(message_id) if metadata else _raw_message(message_id))


def _http_error(status: int, *, retry_after: str | None = None) -> _Outcome:
    return _Outcome(status, {"error": {"message": f"HTTP {status}"}}, retry_after)


def _client(transport: _MockTransport) -> GmailClient:
    client = object.__new__(GmailClient)
    client.service = _MockGmailService(transport)
    return client


def test_raw_batch_retains_successes_and_retries_only_rate_limited_ids() -> None:
    transport = _MockTransport(
        [
            {"a": _ok("a"), "b": _http_error(429, retry_after="0"), "c": _http_error(503, retry_after="0")},
            {"b": _ok("b"), "c": _ok("c")},
        ]
    )

    results, errors = _client(transport).get_messages_raw_batch(["a", "b", "c"], 3, retry_failures=False)

    assert results == [("a", _RAW_BYTES), ("b", _RAW_BYTES), ("c", _RAW_BYTES)]
    assert errors == []
    assert transport.batch_calls == [["a", "b", "c"], ["b", "c"]]


def test_raw_batch_reports_rate_limit_exhaustion_and_unattempted_ids() -> None:
    rate_limit = _http_error(429, retry_after="0")
    transport = _MockTransport([{"a": _ok("a"), "b": rate_limit}, *[{"b": rate_limit} for _ in range(9)]])

    results, errors = _client(transport).get_messages_raw_batch(["a", "b", "c"], 2, retry_failures=False)

    assert results == [("a", _RAW_BYTES)]
    assert [message_id for message_id, _ in errors] == ["b", "c"]
    assert "HTTP 429" in errors[0][1]
    assert "Not fetched" in errors[1][1]
    assert transport.batch_calls == [["a", "b"], *[["b"] for _ in range(9)]]


def test_raw_batch_keeps_individual_retry_for_non_rate_limit_failures() -> None:
    transport = _MockTransport(
        [{"a": _http_error(404), "b": _http_error(403), "c": _ok("c")}],
        individual_responses={"a": [_ok("a")], "b": [_http_error(404)]},
    )

    results, errors = _client(transport).get_messages_raw_batch(["a", "b", "c"], 3)

    assert results == [("a", _RAW_BYTES), ("c", _RAW_BYTES)]
    assert [message_id for message_id, _ in errors] == ["b"]
    assert "Retry failed" in errors[0][1]
    assert transport.batch_calls == [["a", "b", "c"]]
    assert transport.individual_calls == ["a", "b"]


def test_metadata_batch_retries_rate_limited_ids_and_returns_per_id_failures() -> None:
    transport = _MockTransport(
        [{"a": _ok("a", metadata=True), "b": _http_error(503, retry_after="0")}, {"b": _ok("b", metadata=True)}]
    )

    messages, errors = _client(transport).get_messages_metadata_batch(["a", "b"], 2)

    assert [message.id for message in messages] == ["a", "b"]
    assert errors == []
    assert transport.batch_calls == [["a", "b"], ["b"]]


def test_full_message_batch_returns_successes_and_explicit_failures() -> None:
    transport = _MockTransport([{"a": _ok("a"), "b": _http_error(404)}])

    messages, errors = _client(transport).get_messages_batch(["a", "b"], 2)

    assert all(isinstance(message, Email) for message in messages)
    assert [message.id for message in messages] == ["a"]
    assert [message_id for message_id, _ in errors] == ["b"]
    assert "HTTP 404" in errors[0][1]


if __name__ == "__main__":
    import pytest_bazel

    pytest_bazel.main()
