"""Send one runner Command of each kind a test drives to a session."""

from __future__ import annotations

from agentplane.proto import command_pb2
from agentplane.runner.testing.driver import SessionFollow

# The generated protocol stubs' own stub chain, which the mypy aspect resolves for direct deps only.
# gazelle:include_dep @pypi//protobuf


async def submit_input(session: SessionFollow, command_id: str, text: str) -> None:
    await session.command(command_pb2.Command(command_id=command_id, submit_input=command_pb2.SubmitInput(text=text)))


async def interrupt_turn(session: SessionFollow, command_id: str, turn_id: str) -> None:
    await session.command(
        command_pb2.Command(command_id=command_id, interrupt_turn=command_pb2.InterruptTurn(turn_id=turn_id))
    )


async def change_model(session: SessionFollow, command_id: str, model: str) -> None:
    await session.command(command_pb2.Command(command_id=command_id, change_model=command_pb2.ChangeModel(model=model)))


async def change_reasoning_effort(session: SessionFollow, command_id: str, effort: str) -> None:
    await session.command(
        command_pb2.Command(
            command_id=command_id, change_reasoning_effort=command_pb2.ChangeReasoningEffort(effort=effort)
        )
    )
