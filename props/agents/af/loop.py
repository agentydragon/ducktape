"""Drive a MAF agent to completion with reminder-on-text and terminal-tool stop.

This is the only control behavior that isn't a single middleware: MAF's `agent.run()`
drives the tool-calling loop until the model emits a plain text answer (no tool call) —
the natural end of a burst. If the agent isn't `done()` at that point, the model answered
in prose instead of acting, so we re-prompt with a reminder on the same session.
A terminal tool ends a burst early via `terminate_after_tools`; every burst re-checks `done()`.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

from agent_framework import Agent, FunctionTool, MiddlewareTermination, MiddlewareTypes

from props.agents.af.client import build_chat_client_from_env
from props.db.database import Database


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


async def run_until_done(agent: Agent, *, done: Callable[[], bool], reminder: str) -> None:
    """Run `agent` until `done()` returns true.

    The session persists history across bursts via MAF's in-memory history provider.
    """
    session = agent.create_session()
    message = "Begin."
    while not done():
        try:
            await agent.run(message, session=session)
        except MiddlewareTermination:
            return  # a terminal tool fired; done() reflects it
        message = reminder
