"""The cluster-internal read side: recent decisions per subject, and health."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Literal

from fastapi import FastAPI, HTTPException, Response, status
from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel

from agentplane.egress.decision_log import DB_ERRORS, DecisionLog
from agentplane.egress.decisions import DecisionRecord
from agentplane.egress.policy import Index, resolve_binding
from agentplane.egress.resources import ActiveReason
from agentplane.subjects import ServiceAccountRef

logger = logging.getLogger(__name__)


class _CamelModel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, validate_by_name=True, serialize_by_alias=True)


class Health(_CamelModel):
    synced: bool
    draining: bool
    decision_history: dict[str, int | bool | None]
    stale_after_seconds: float
    refreshed_seconds_ago: dict[str, float]


class BindingObservation(_CamelModel):
    name: str
    uid: str | None
    generation: int | None
    reason: ActiveReason
    policies: list[str]
    missing_policies: list[str]


class Bindings(_CamelModel):
    scope: Literal["replica-local"] = "replica-local"
    observed_at: datetime
    available: bool
    bindings: list[BindingObservation]


class Liveness(BaseModel):
    alive: bool = True


def create_admin_app(
    decision_log: DecisionLog, index: Index, *, clock: Callable[[], datetime] = lambda: datetime.now(UTC)
) -> FastAPI:
    app = FastAPI(title="agentplane-egress-admin")

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

    @app.get("/bindings")
    async def bindings() -> Bindings:
        now = clock()
        return Bindings(
            observed_at=now,
            available=index.available(now),
            bindings=[
                BindingObservation(
                    name=resolution.binding.metadata.name,
                    uid=resolution.binding.metadata.uid,
                    generation=resolution.binding.metadata.generation,
                    reason=resolution.reason,
                    policies=[policy.metadata.name for policy in resolution.policies],
                    missing_policies=list(resolution.missing),
                )
                for resolution in (
                    resolve_binding(index, binding, now)
                    for binding in sorted(index.bindings.values(), key=lambda binding: binding.metadata.name)
                )
            ],
        )

    @app.get("/livez")
    async def livez() -> Liveness:
        return Liveness()

    return app
