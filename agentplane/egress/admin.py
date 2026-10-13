"""The cluster-internal read side: recent decisions per subject, when they change, and health."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime

from fastapi import FastAPI, HTTPException, Response, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel

from agentplane.egress.decision_changes import DecisionChanges
from agentplane.egress.decision_log import DB_ERRORS, DecisionLog
from agentplane.egress.decisions import DecisionRecord
from agentplane.egress.policy import Index
from agentplane.sse import frame, sse_response
from agentplane.subjects import ServiceAccountRef

logger = logging.getLogger(__name__)

# Seconds of quiet after which `/decisions/changes` sends a frame anyway, so a reader whose connection
# died silently finds out by its own read timeout instead of showing a frozen log as live.
CHANGES_HEARTBEAT_S = 15


class _CamelModel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, validate_by_name=True, serialize_by_alias=True)


class Health(_CamelModel):
    synced: bool
    draining: bool
    decision_history: dict[str, int | bool | None]
    stale_after_seconds: float
    refreshed_seconds_ago: dict[str, float]


class Liveness(BaseModel):
    alive: bool = True


def create_admin_app(
    decision_log: DecisionLog, index: Index, *, clock: Callable[[], datetime] = lambda: datetime.now(UTC)
) -> FastAPI:
    app = FastAPI(title="agentplane-egress-admin")
    decision_changes = DecisionChanges(decision_log.store.engine.url, heartbeat_s=CHANGES_HEARTBEAT_S)

    @app.get("/decisions/changes")
    async def changes() -> StreamingResponse:
        """A `changed` frame whenever decisions may have been committed, and at least every heartbeat. It
        carries nothing: the reader re-reads `/decisions`, which stays the authority."""

        async def frames() -> AsyncIterator[bytes]:
            async for _ in decision_changes.follow():
                yield frame("changed", {})

        return sse_response(frames())

    @app.get("/decisions")
    async def decisions(namespace: str | None = None, name: str | None = None) -> list[DecisionRecord]:
        """`?namespace=&name=` selects one subject; neither selects the refusals that never authenticated."""
        if (namespace is None) != (name is None):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "namespace and name are given together or not at all")
        subject = None if namespace is None or name is None else ServiceAccountRef(namespace=namespace, name=name)
        try:
            return await decision_log.store.recent(subject)
        except DB_ERRORS as error:
            logger.warning("decision history read failed (%s)", type(error).__name__)
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "decision-history-unavailable") from None

    @app.get("/healthz")
    async def healthz(response: Response) -> Health:
        """Healthy means the index is both complete and still moving.

        `synced` alone said only that every kind had been listed once, so a proxy whose watches had
        since wedged went on answering 200 while it served a frozen snapshot of the rules -- the whole
        point of a policy engine being wrong in the quiet direction. The ages below are the facts; the
        status code is the verdict on them.
        """
        now = clock()
        if not index.available(now):
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return Health(
            synced=index.synced,
            draining=index.draining,
            decision_history=decision_log.health(),
            stale_after_seconds=index.freshness.stale_after_seconds,
            refreshed_seconds_ago={plural: round(age, 1) for plural, age in index.freshness.ages(now).items()},
        )

    @app.get("/livez")
    async def livez() -> Liveness:
        return Liveness()

    return app
