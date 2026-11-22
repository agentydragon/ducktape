"""Drive a scripted model turn through an exact Dispatcher admission and its recorded History."""

import asyncio
from typing import Any, Protocol
from uuid import uuid4

from agentplane.proto import command_pb2, event_pb2
from agentplane.session_dispatcher.dispatcher.api.session import session_dispatcher_pb2
from agentplane.system_tests.driver.turn import Turn
from agentplane.testing.scripted_model import ModelRequest, ScriptedModel, ShellCall, Text

# gazelle:include_dep @pypi//protobuf


class TurnSession(Protocol):
    async def next_turn(self, *, awaiting_command_id: str | None = None) -> Turn: ...
    async def submit_input(self, command_id: str, text: str) -> session_dispatcher_pb2.Dispatch: ...


async def drive_scripted_turn(
    agent: TurnSession,
    *,
    command_id: str,
    prompt: str,
    model: ScriptedModel[Any],
    shell_command: str | None,
    shell_call_id: str | None,
    response: str,
) -> tuple[Turn, ModelRequest[Any]]:
    async with asyncio.TaskGroup() as turns:
        collecting = turns.create_task(agent.next_turn(awaiting_command_id=command_id))
        admission = await agent.submit_input(command_id, prompt)
        assert admission.command_id == command_id
        assert admission.operation == command_pb2.OPERATION_SUBMIT_INPUT

        request = await model.request()
        assert prompt in request.user_texts
        if shell_command is None:
            await model.reply(request, Text(response))
        else:
            call_id = shell_call_id or f"call-{uuid4().hex[:8]}"
            await model.reply(request, ShellCall(call_id=call_id, command=shell_command))
            tool_result = await model.request()
            try:
                assert any(output.call_id == call_id for output in tool_result.tool_outputs), tool_result.tool_outputs
            finally:
                # Complete the scripted turn even when the tool result is unexpected.
                await model.reply(tool_result, Text(response))
    turn = collecting.result()
    assert turn.status == event_pb2.TURN_STATUS_COMPLETED
    return turn, request
