"""The cluster-internal read side: recent decisions per sandbox, and health."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from aiohttp import web
from more_itertools import one

from x.agentplane.egress.decisions import DecisionRing
from x.agentplane.egress.policy import Index

_RING = web.AppKey("ring", DecisionRing)
_INDEX = web.AppKey("index", Index)
_STALE_AFTER = web.AppKey("stale_after", float)
_CLOCK: web.AppKey[Callable[[], datetime]] = web.AppKey("clock")

# How many resync periods a kind may miss before the proxy calls itself unhealthy. One late cycle
# is a slow API server; three in a row is a wedge, and the probe restarting the pod is the remedy.
STALE_AFTER_CYCLES = 3


async def _decisions(request: web.Request) -> web.Response:
    decisions = request.app[_RING].recent(request.query.get("sandbox"))
    return web.json_response([decision.model_dump(mode="json") for decision in decisions])


async def _healthz(request: web.Request) -> web.Response:
    """Healthy means the index is both complete and still moving.

    `synced` alone said only that every kind had been listed once, so a proxy whose watches had
    since wedged went on answering 200 while it served a frozen snapshot of the rules -- the whole
    point of a policy engine being wrong in the quiet direction. The ages below are the facts; the
    status code is the verdict on them.
    """
    index, stale_after = request.app[_INDEX], request.app[_STALE_AFTER]
    now = request.app[_CLOCK]()
    ages = {plural: (now - at).total_seconds() for plural, at in index.refreshed.items()}
    healthy = index.synced and all(age <= stale_after for age in ages.values())
    return web.json_response(
        {
            "synced": index.synced,
            "staleAfterSeconds": stale_after,
            "refreshedSecondsAgo": {plural: round(age, 1) for plural, age in sorted(ages.items())},
        },
        status=200 if healthy else 503,
    )


def create_admin_app(
    ring: DecisionRing, index: Index, *, resync_seconds: int, clock: Callable[[], datetime] = lambda: datetime.now(UTC)
) -> web.Application:
    app = web.Application()
    app[_RING] = ring
    app[_INDEX] = index
    app[_STALE_AFTER] = float(resync_seconds * STALE_AFTER_CYCLES)
    app[_CLOCK] = clock
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
