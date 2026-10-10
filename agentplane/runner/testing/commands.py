"""Send one runner Command of each kind a test drives over an attachment."""

from __future__ import annotations

from agentplane.protocol import command_pb2
from agentplane.runner.client import Attachment

# The generated protocol stubs' own stub chain, which the mypy aspect resolves for direct deps only.
# gazelle:include_dep @pypi//protobuf


async def submit_input(attachment: Attachment, command_id: str, text: str) -> None:
    await attachment.command(
        command_pb2.Command(command_id=command_id, submit_input=command_pb2.SubmitInput(text=text))
    )


async def interrupt_turn(attachment: Attachment, command_id: str, turn_id: str) -> None:
    await attachment.command(
        command_pb2.Command(command_id=command_id, interrupt_turn=command_pb2.InterruptTurn(turn_id=turn_id))
    )


async def change_model(attachment: Attachment, command_id: str, model: str) -> None:
    await attachment.command(
        command_pb2.Command(command_id=command_id, change_model=command_pb2.ChangeModel(model=model))
    )


async def change_reasoning_effort(attachment: Attachment, command_id: str, effort: str) -> None:
    await attachment.command(
        command_pb2.Command(
            command_id=command_id, change_reasoning_effort=command_pb2.ChangeReasoningEffort(effort=effort)
        )
    )
