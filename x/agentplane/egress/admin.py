"""The cluster-internal read side: recent decisions per subject, and health."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from aiohttp import web
from more_itertools import one

from x.agentplane.egress.decision_log import DB_ERRORS, DecisionLog
from x.agentplane.egress.policy import STALE_AFTER_CYCLES, Index, resolve_binding
from x.agentplane.subjects import SubjectKind, SubjectView

logger = logging.getLogger(__name__)

_DECISION_LOG = web.AppKey("decision_log", DecisionLog)
_INDEX = web.AppKey("index", Index)
_STALE_AFTER = web.AppKey("stale_after", float)
_CLOCK: web.AppKey[Callable[[], datetime]] = web.AppKey("clock")


async def _decisions(request: web.Request) -> web.Response:
    """`?kind=&name=` selects one subject; neither selects the refusals that never authenticated."""
    kind, name = request.query.get("kind"), request.query.get("name")
    if (kind is None) != (name is None):
        raise web.HTTPBadRequest(reason="kind and name are given together or not at all")
    subject = None if kind is None or name is None else SubjectView(kind=SubjectKind(kind), name=name)
    try:
        decisions = await request.app[_DECISION_LOG].store.recent(subject)
    except DB_ERRORS as error:
        logger.warning("decision history read failed (%s)", type(error).__name__)
        return web.json_response({"error": "decision-history-unavailable"}, status=503)
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
    healthy = index.available(now, stale_after_seconds=stale_after)
    return web.json_response(
        {
            "synced": index.synced,
            "draining": index.draining,
            "decisionHistory": request.app[_DECISION_LOG].health(),
            "staleAfterSeconds": stale_after,
            "refreshedSecondsAgo": {plural: round(age, 1) for plural, age in sorted(ages.items())},
        },
        status=200 if healthy else 503,
    )


async def _bindings(request: web.Request) -> web.Response:
    index = request.app[_INDEX]
    now = request.app[_CLOCK]()
    return web.json_response(
        {
            "scope": "replica-local",
            "observedAt": now.isoformat(),
            "available": index.available(now, stale_after_seconds=request.app[_STALE_AFTER]),
            "bindings": [
                {
                    "name": resolution.binding.metadata.name,
                    "uid": resolution.binding.metadata.uid,
                    "generation": resolution.binding.metadata.generation,
                    "reason": resolution.reason,
                    "policies": [policy.metadata.name for policy in resolution.policies],
                    "missingPolicies": list(resolution.missing),
                }
                for resolution in (
                    resolve_binding(index, binding, now)
                    for binding in sorted(index.bindings.values(), key=lambda binding: binding.metadata.name)
                )
            ],
        }
    )


async def _livez(request: web.Request) -> web.Response:
    return web.json_response({"alive": True})


def create_admin_app(
    decision_log: DecisionLog,
    index: Index,
    *,
    resync_seconds: int,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> web.Application:
    app = web.Application()
    app[_DECISION_LOG] = decision_log
    app[_INDEX] = index
    app[_STALE_AFTER] = float(resync_seconds * STALE_AFTER_CYCLES)
    app[_CLOCK] = clock
    app.router.add_get("/decisions", _decisions)
    app.router.add_get("/bindings", _bindings)
    app.router.add_get("/healthz", _healthz)
    app.router.add_get("/livez", _livez)
    return app


@asynccontextmanager
async def serve_admin(app: web.Application, host: str, port: int) -> AsyncIterator[int]:
    """Serve `app`, yielding the bound port (pass 0 for an ephemeral one)."""
    runner = web.AppRunner(app, shutdown_timeout=5)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    try:
        yield one(runner.addresses)[1]
    finally:
        await runner.cleanup()
