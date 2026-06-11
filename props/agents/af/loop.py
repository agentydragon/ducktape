"""Drive a MAF agent to completion with reminder-on-text and terminal-tool stop.

This is the only control behavior that isn't a single middleware: MAF's `agent.run()`
drives the tool-calling loop until the model emits a plain text answer (no tool call) —
the natural end of a burst. If the agent isn't `done()` at that point, the model answered
in prose instead of acting, so we re-prompt with a reminder on the same session (the
reminder-on-text behavior, faithfully — and cleaner than agent_core's in-loop injection).
A terminal tool ends a burst early via `terminate_after_tools`; every burst re-checks `done()`.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence

from agent_framework import Agent, FunctionTool, MiddlewareTermination, MiddlewareTypes

from props.agents.af.client import build_chat_client_from_env
from props.db.database import Database

logger = logging.getLogger(__name__)


def make_agent(
    db: Database, *, instructions: str, tools: list[FunctionTool], middleware: Sequence[MiddlewareTypes]
) -> Agent:
    """Build a props MAF agent for the current run (client + default options selected by the
    model's api_shape — see build_chat_client; e.g. `store=False` only for the Responses shape)."""
    setup = build_chat_client_from_env(db)
    return Agent(
        client=setup.client,
        instructions=instructions,
        tools=tools,
        middleware=middleware,
        default_options=setup.default_options,
    )


async def run_until_done(
    agent: Agent, *, done: Callable[[], bool], reminder: str, kickoff: str = "Begin.", max_turns: int | None = None
) -> None:
    """Run `agent` until `done()` returns true (or `max_turns` bursts elapse).

    The session persists history across bursts via MAF's in-memory history provider.
    """
    session = agent.create_session()
    message = kickoff
    turn = 0
    while not done():
        if max_turns is not None and turn >= max_turns:
            logger.warning("run_until_done hit max_turns=%d without completion", max_turns)
            return
        turn += 1
        try:
            await agent.run(message, session=session)
        except MiddlewareTermination:
            return  # a terminal tool fired; done() reflects it
        message = reminder
