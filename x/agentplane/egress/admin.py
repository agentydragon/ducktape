"""The cluster-internal read side: recent decisions per sandbox, and health."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from aiohttp import web
from more_itertools import one

from x.agentplane.egress.decisions import DecisionRing
from x.agentplane.egress.policy import Index

_RING = web.AppKey("ring", DecisionRing)
_INDEX = web.AppKey("index", Index)


async def _decisions(request: web.Request) -> web.Response:
    decisions = request.app[_RING].recent(request.query.get("sandbox"))
    return web.json_response([decision.model_dump(mode="json") for decision in decisions])


async def _healthz(request: web.Request) -> web.Response:
    synced = request.app[_INDEX].synced
    return web.json_response({"synced": synced}, status=200 if synced else 503)


def create_admin_app(ring: DecisionRing, index: Index) -> web.Application:
    app = web.Application()
    app[_RING] = ring
    app[_INDEX] = index
    app.router.add_get("/decisions", _decisions)
    app.router.add_get("/healthz", _healthz)
    return app


@asynccontextmanager
async def serve_admin(app: web.Application, host: str, port: int) -> AsyncIterator[int]:
    """Serve `app`, yielding the bound port (pass 0 for an ephemeral one)."""
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    try:
        yield one(runner.addresses)[1]
    finally:
        await runner.cleanup()
