"""Offline acceptance-driver startup checks, not deployed harness evidence."""

from unittest.mock import Mock, create_autospec
from uuid import UUID

import httpx
import pytest
import pytest_bazel

from x.agentplane.acceptance.agent import Agent
from x.agentplane.app.client import Attachment, Client
from x.agentplane.runner import protocol_pb2

# gazelle:include_dep @pypi//protobuf


async def test_agent_open_retries_unavailable_without_creating_another_session() -> None:
    request = httpx.Request("POST", "https://app.test.invalid/sandboxes/test-sandbox/sessions")
    unavailable = httpx.HTTPStatusError(
        "runner is not listening", request=request, response=httpx.Response(503, request=request)
    )
    client = create_autospec(Client, instance=True)
    client.open_session.side_effect = [
        unavailable,
        Attachment(protocol_pb2.Attached(session_id="test-session", last_cursor=7)),
    ]
    client.thread.return_value = Mock(id=UUID("00000000-0000-4000-8000-000000000001"))

    await Agent.open(client, sandbox="test-sandbox", harness=protocol_pb2.HARNESS_CODEX, model="test-model")

    first, retry = client.open_session.call_args_list
    assert first == retry
    assert first.args[0] == "test-sandbox"
    assert first.args[1].startswith("acceptance-")
    assert first.args[2].model == "test-model"
    client.thread.assert_awaited_once_with("test-sandbox", "test-session")


@pytest.mark.parametrize("status", [409, 500, 502])
async def test_startup_refusals_and_other_server_failures_are_not_retried(status: int) -> None:
    request = httpx.Request("POST", "https://app.test.invalid/sandboxes/test-sandbox/sessions")
    response = httpx.Response(status, request=request)
    client = create_autospec(Client, instance=True)
    client.open_session.side_effect = httpx.HTTPStatusError("open refused", request=request, response=response)
    with pytest.raises(httpx.HTTPStatusError) as caught:
        await Agent.open(client, sandbox="test-sandbox", harness=protocol_pb2.HARNESS_CODEX, model="test-model")
    assert caught.value.response is response
    client.open_session.assert_awaited_once()
    client.thread.assert_not_awaited()


if __name__ == "__main__":
    pytest_bazel.main()
