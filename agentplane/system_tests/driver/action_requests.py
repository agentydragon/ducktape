"""Decode and settle Action receipts through the operator-facing BFF."""

from __future__ import annotations

from http import HTTPStatus
from uuid import uuid4

import httpx
import pytest
from google.protobuf import json_format
from google.protobuf.struct_pb2 import Value

from agentplane.action_service.api.contracts import action_service_pb2, operator_action_service_pb2
from agentplane.system_tests.driver.tracing import tracer


def action_request(response: httpx.Response) -> action_service_pb2.ActionRequest:
    """Decode an Actions facade response without rendering identity details on parse failure."""
    __tracebackhide__ = True
    try:
        envelope = action_service_pb2.GetActionRequestResponse()
        json_format.Parse(response.text, envelope)
        if not envelope.HasField("action_request"):
            raise ValueError("Action Service response omitted its request")
        return envelope.action_request
    except json_format.ParseError, ValueError:
        pytest.fail("BFF returned an invalid ActionRequest; response body withheld", pytrace=False)


def decision_response(response: httpx.Response) -> action_service_pb2.ActionRequest:
    """Decode an operator decision response without rendering identity details on parse failure."""
    __tracebackhide__ = True
    try:
        envelope = operator_action_service_pb2.DecideActionRequestResponse()
        json_format.Parse(response.text, envelope)
        if not envelope.HasField("action_request"):
            raise ValueError("Action Service response omitted its request")
        return envelope.action_request
    except json_format.ParseError, ValueError:
        pytest.fail("BFF returned an invalid decision response; response body withheld", pytrace=False)


async def decide_pending_action(
    operator_bff: httpx.AsyncClient, request_id: str, verdict: action_service_pb2.Verdict
) -> action_service_pb2.ActionRequest:
    """Confirm a request is pending, then decide it so no operator-path request lingers."""
    with tracer.start_as_current_span("system_test.mcp.operator_decision"):
        path = f"/upstream/actions/v1/action-requests/{request_id}"
        receipt = action_request(await operator_bff.get(path))
        assert receipt.state == action_service_pb2.ACTION_STATE_DECISION_PENDING
        if receipt.HasField("decision"):
            pytest.fail("Request expected on the human path already has a Decision", pytrace=False)
        decision = operator_action_service_pb2.DecideActionRequestRequest(
            request_id=str(request_id), verdict=verdict, expected_version=receipt.version, idempotency_key=str(uuid4())
        )
        response = await operator_bff.post(
            f"/upstream/actions/v1/operator/action-requests/{request_id}/decision",
            json=json_format.MessageToDict(decision),
        )
        assert response.status_code == HTTPStatus.OK
        decided = decision_response(response)
        assert decided.state == (
            action_service_pb2.ACTION_STATE_ALLOWED
            if verdict == action_service_pb2.VERDICT_ALLOW
            else action_service_pb2.ACTION_STATE_DENIED
        )
        assert decided.HasField("decision")
        assert decided.decision.verdict == verdict
        return receipt


def assert_echoed_result(result: Value, message: str) -> None:
    """Check the structured result recorded by the MCP echo tool."""
    result_json = json_format.MessageToDict(result)
    assert isinstance(result_json, dict), result_json
    assert (result_json["content"], result_json.get("isError", False)) == (
        [{"type": "text", "text": f"Echo: {message}"}],
        False,
    )
