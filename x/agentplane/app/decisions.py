"""The proxy's recent decisions for one subject, read off its cluster-internal admin port.

The proxy reads shared PostgreSQL history at `GET /decisions?namespace=<ns>&name=<name>`: a subject
is a ServiceAccount, and the same name in two namespaces is two of them.
Database read failures are explicit non-success responses, surfaced as DecisionsUnavailableError.
"""

from __future__ import annotations

import logging
from datetime import datetime
from enum import StrEnum

import httpx
from pydantic import BaseModel, ConfigDict, Field

from x.agentplane.subjects import ServiceAccountRef

logger = logging.getLogger(__name__)


class Outcome(StrEnum):
    ALLOW = "allow"
    DENY = "deny"


class Decision(BaseModel):
    """One admission as the proxy served it; names only, never credential values."""

    # The proxy is deployed separately and may add fields first; only what is shown is read.
    model_config = ConfigDict(extra="ignore")

    at: datetime
    method: str
    host: str
    port: int
    path: str | None = Field(default=None, description="Absent for a CONNECT.")
    outcome: Outcome
    reason: str | None = Field(default=None, description="The proxy's machine-readable refusal, on a deny.")
    binding: str | None = None
    policy: str | None = None
    rule: int | None = None
    substituted: bool = Field(default=False, description="Whether a credential replaced a placeholder.")
    address: str | None = Field(
        default=None, description="The address the proxy resolved the host to and dialled, when admitted."
    )


class DecisionsUnavailableError(Exception):
    """The proxy did not answer: not deployed yet, restarting, or refused by network policy."""


class DecisionsClient:
    def __init__(self, http: httpx.AsyncClient):
        self._http = http

    async def recent(self, subject: ServiceAccountRef) -> list[Decision]:
        try:
            response = await self._http.get("/decisions", params={"namespace": subject.namespace, "name": subject.name})
            response.raise_for_status()
        except httpx.HTTPError as error:
            logger.warning("egress proxy decisions unavailable for %s/%s: %s", subject.namespace, subject.name, error)
            raise DecisionsUnavailableError(f"the egress proxy did not answer: {error}") from error
        return [Decision.model_validate(item) for item in response.json()]
