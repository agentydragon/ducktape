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
    """The proxy did not answer (not deployed yet, restarting, refused by network policy) or answered non-success."""

    def __init__(self, *, status_code: int | None, detail: str) -> None:
        super().__init__(
            f"the egress proxy did not answer: {detail}"
            if status_code is None
            else f"the egress proxy answered {status_code}: {detail}"
        )


class DecisionsClient:
    def __init__(self, http: httpx.AsyncClient):
        self._http = http

    async def recent(self, subject: ServiceAccountRef) -> list[Decision]:
        try:
            response = await self._http.get("/decisions", params={"namespace": subject.namespace, "name": subject.name})
        except httpx.HTTPError as error:
            logger.warning("egress proxy decisions unavailable for %s/%s: %s", subject.namespace, subject.name, error)
            raise DecisionsUnavailableError(status_code=None, detail=str(error)) from error
        if not response.is_success:
            logger.warning(
                "egress proxy decisions failed for %s/%s: %s %s",
                subject.namespace,
                subject.name,
                response.status_code,
                response.text,
            )
            raise DecisionsUnavailableError(status_code=response.status_code, detail=response.text)
        return [Decision.model_validate(item) for item in response.json()]
