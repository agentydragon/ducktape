"""Envelope parsing uses the generated Codex app-server models."""

from __future__ import annotations

import pytest_bazel

from haku.runner.codex.generated_protocol import JSONRPCError, JSONRPCNotification, JSONRPCRequest
from haku.runner.codex.protocol import UnknownMessage, parse_message


def test_parse_message_returns_generated_request_model() -> None:
    message = parse_message({"id": "request-1", "method": "future/method", "params": []})

    assert isinstance(message, JSONRPCRequest)
    assert message.id == "request-1"
    assert message.method == "future/method"


def test_parse_message_returns_generated_notification_model_for_future_methods() -> None:
    message = parse_message({"method": "future/notification", "params": []})

    assert isinstance(message, JSONRPCNotification)
    assert message.method == "future/notification"


def test_parse_message_returns_generated_error_model() -> None:
    message = parse_message({"id": 7, "error": {"code": -1, "message": "nope"}})

    assert isinstance(message, JSONRPCError)
    assert message.id == 7
    assert message.error.message == "nope"


def test_parse_message_keeps_invalid_envelopes_fail_soft() -> None:
    message = parse_message({"id": True, "method": "future/method"})

    assert isinstance(message, UnknownMessage)


if __name__ == "__main__":
    pytest_bazel.main()
