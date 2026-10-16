"""Send each typed Runner command operation used by tests to a session."""

from __future__ import annotations

from agentplane.runner.testing.driver import SessionFollow


async def submit_input(session: SessionFollow, command_id: str, text: str) -> None:
    await session.submit_input(command_id, text)


async def interrupt_turn(session: SessionFollow, command_id: str, turn_id: str) -> None:
    await session.interrupt_turn(command_id, turn_id)


async def change_model(session: SessionFollow, command_id: str, model: str) -> None:
    await session.change_model(command_id, model)


async def change_reasoning_effort(session: SessionFollow, command_id: str, effort: str) -> None:
    await session.change_reasoning_effort(command_id, effort)
