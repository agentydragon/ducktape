"""Gateway recovery evidence survives code-mode wait boundaries."""

import json
from dataclasses import replace
from typing import cast
from unittest.mock import AsyncMock, create_autospec

import pytest

from agentplane.native.codex.testing.requests import ResponsesRequest
from agentplane.native.testing.model_endpoint import ModelExchange
from agentplane.system_tests.scenarios import central_sampler
from agentplane.testing.codex_model import CodexModel
from agentplane.testing.scripted_model import ModelRequest, ToolOutput

_NOTICE = (
    "The retained exec-server session was definitively lost. Its live process handles have been abandoned; "
    "their remote state is unknown unless explicitly reported, and no mutation was replayed."
)


def request(call_id: str, status: str) -> ModelRequest[ResponsesRequest]:
    exchange = create_autospec(ModelExchange, instance=True)
    exchange.__aenter__.return_value = exchange
    return ModelRequest(
        _exchange=exchange,
        model="model",
        effort=None,
        system_text="",
        user_texts=[],
        assistant_texts=[],
        reasoning_texts=[],
        tool_outputs=[ToolOutput(call_id, [status])],
        tool_calls=[],
        streaming=True,
    )


@pytest.mark.parametrize("notice_channel", ["earlier tool", "developer", "user", "final tool"])
async def test_follow_cell_preserves_notice_across_wait_call_ids(notice_channel: str) -> None:
    initial = request("probe", "Script running with cell ID retained-cell")
    waiting = request("probe-wait-1", "Script running with cell ID retained-cell")
    failure = 'Script failed\nretained exec-server session "native-session" was definitively lost'
    settled = request("probe-wait-1-wait-2", failure)
    match notice_channel:
        case "earlier tool":
            # A late notice in the final request still names the preceding wait.
            settled.tool_outputs.append(ToolOutput("probe-wait-1", [_NOTICE]))
        case "developer":
            waiting = replace(waiting, system_text=_NOTICE)
        case "user":
            waiting.user_texts.append(_NOTICE)
        case "final tool":
            settled.tool_outputs.append(ToolOutput("probe-wait-1-wait-2", [_NOTICE]))
    model: CodexModel = create_autospec(CodexModel, instance=True)
    cast(AsyncMock, model.request).side_effect = [waiting, settled]

    final_request, output_text, visible, final_output = await central_sampler._follow_code_mode_cell(
        model, initial, "probe", timeout_s=1
    )

    assert final_request is settled
    assert final_output is settled.tool_outputs[0]
    assert final_output.blocks == [failure]
    assert failure in output_text
    assert _NOTICE in visible
    assert cast(AsyncMock, model.request).await_count == 2
    for previous, call_id in [(initial, "probe-wait-1"), (waiting, "probe-wait-1-wait-2")]:
        send = cast(AsyncMock, previous._exchange.send)
        send.assert_awaited_once()
        assert send.await_args is not None
        (item,) = [
            json.loads(event.data.removeprefix(b"data: "))["item"]
            for event in send.await_args.args
            if event.kind == "response.output_item.done"
        ]
        assert item["name"] == "wait"
        assert item["call_id"] == call_id
        assert json.loads(item["arguments"])["cell_id"] == "retained-cell"
