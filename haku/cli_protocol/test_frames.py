"""The control-channel models serialize what the CLI accepts and parse what it sends.

Payloads here are captured verbatim from probe runs (claude-code 2.1.220). They are the corpus a
version repin has to keep passing — which catches a renamed or retyped field, though not a
changed behaviour; that needs the probes.
"""

from __future__ import annotations

import pytest_bazel

from haku.cli_protocol.frames import (
    ControlRequestFrame,
    ControlResponse,
    ControlSubtype,
    InitializeRequest,
    InterruptRequest,
)


def test_the_handshake_serializes_to_the_camel_case_the_cli_reads() -> None:
    """A snake_case key would be accepted and ignored, since `initialize` rejects nothing."""
    request = InitializeRequest(sdk_mcp_servers=["probe"], forward_subagent_text=True, json_schema={"type": "object"})

    assert request.model_dump(exclude_none=True) == {
        "subtype": "initialize",
        "sdkMcpServers": ["probe"],
        "forwardSubagentText": True,
        "jsonSchema": {"type": "object"},
    }


def test_an_omitted_field_is_absent_rather_than_null() -> None:
    assert InitializeRequest().model_dump(exclude_none=True) == {"subtype": "initialize"}


def test_cancelling_the_queue_is_explicit() -> None:
    """`interrupt` alone leaves the queue to run, so an abort that means it has to say so."""
    assert InterruptRequest(reason="user-cancel", cancel_queued=True).model_dump(exclude_none=True) == {
        "subtype": "interrupt",
        "reason": "user-cancel",
        "cancel_queued": True,
    }


def test_each_request_gets_its_own_id() -> None:
    assert ControlRequestFrame(request={}).request_id != ControlRequestFrame(request={}).request_id


def test_the_correlation_key_comes_from_inside_the_response() -> None:
    frame = {
        "type": "control_response",
        "response": {"subtype": "success", "request_id": "req_1", "response": {"commands": []}},
    }

    response = ControlResponse.model_validate(frame["response"])

    assert response.request_id == "req_1"
    assert response.subtype is ControlSubtype.SUCCESS


def test_a_rejected_handshake_carries_its_reason() -> None:
    response = ControlResponse.model_validate(
        {"subtype": "error", "request_id": "req_1", "error": "initialize: skills must be an array of strings"}
    )

    assert response.subtype is ControlSubtype.ERROR
    assert "skills" in (response.error or "")


if __name__ == "__main__":
    pytest_bazel.main()
