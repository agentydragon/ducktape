"""The proxy's recent decisions for one subject, read off its cluster-internal admin port.

The proxy reads shared PostgreSQL history at `GET /decisions?namespace=<ns>&name=<name>`: a subject
is a ServiceAccount, and the same name in two namespaces is two of them.
Database read failures are explicit non-success responses, surfaced as DecisionsUnavailableError.
"""

from __future__ import annotations

import logging

import httpx

from agentplane.egress.decisions import Decision
from agentplane.subjects import ServiceAccountRef

logger = logging.getLogger(__name__)


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
