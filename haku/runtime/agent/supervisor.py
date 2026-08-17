"""Long-lived Haku supervisor: one warm Agent Framework session, woken by events.

A single in-process `AgentSession` stays warm across wakes for the pod's lifetime, so
the manual + run procedure the agent reads on the first wake stay in context and aren't
re-read each wake (the expensive part); `SummarizationStrategy` bounds the history. When
`HAKU_REDIS_URL` is set, session history persists in Valkey keyed by the session id, so
it survives pod restarts too; otherwise a restart re-orients from haku-state (git is the
durable memory regardless). Wakes fire from a scheduler tick or `POST /wake`, one at a
time.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

import uvicorn
from agent_framework import Agent, AgentSession
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from fastapi import FastAPI

from haku.runtime.agent.agent import WAKE, aclose_history, build_agent, build_history_provider, build_mcp_tools
from haku.runtime.agent.bootstrap import bootstrap
from haku.runtime.agent.config import Settings

logger = logging.getLogger(__name__)


@dataclass
class HakuRuntime:
    """The warm agent + its single long-lived session, serialized by a one-wake lock."""

    agent: Agent
    session: AgentSession
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def wake(self, message: str = WAKE) -> str:
        async with self.lock:
            logger.info("wake: running one scan pass")
            response = await self.agent.run(message, session=self.session)
            text = response.text or ""
            logger.info("wake complete: %s", text[:500])
            return text


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = Settings()
    await asyncio.to_thread(bootstrap, settings)
    mcp_tools = build_mcp_tools(settings)
    history = build_history_provider(settings)
    async with contextlib.AsyncExitStack() as stack:
        # Open the remote MCP toolsets once, for the app's lifetime, rather than per wake.
        for tool in mcp_tools:
            await stack.enter_async_context(tool)
        runtime = HakuRuntime(
            agent=build_agent(settings, mcp_tools, history), session=AgentSession(session_id=settings.session_id)
        )
        app.state.runtime = runtime

        scheduler = AsyncIOScheduler()
        if settings.wake_interval_seconds > 0:
            # coalesce + max_instances=1: never pile up or overlap scheduled wakes.
            scheduler.add_job(
                runtime.wake, IntervalTrigger(seconds=settings.wake_interval_seconds), max_instances=1, coalesce=True
            )
            scheduler.start()
            logger.info("scheduler started: wake every %ds", settings.wake_interval_seconds)
        try:
            yield
        finally:
            if scheduler.running:
                scheduler.shutdown(wait=False)
            await aclose_history(history)


def create_app() -> FastAPI:
    app = FastAPI(title="haku-agent", lifespan=lifespan)

    @app.post("/wake")
    async def wake() -> dict[str, str]:
        runtime: HakuRuntime = app.state.runtime
        return {"result": await runtime.wake()}

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    settings = Settings()
    uvicorn.run(create_app(), host=settings.host, port=settings.port)


if __name__ == "__main__":
    main()
