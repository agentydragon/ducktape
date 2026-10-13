"""The proxy's recent decisions for one subject, read off its cluster-internal admin port.

The proxy reads shared PostgreSQL history at `GET /decisions?namespace=<ns>&name=<name>`: a subject
is a ServiceAccount, and the same name in two namespaces is two of them.
Database read failures are explicit non-success responses, surfaced as DecisionsUnavailableError.
`GET /decisions/changes` says when to read again; `watch` follows it.
"""

from __future__ import annotations

import asyncio
import logging

import httpx

from agentplane.changes import ChangeSignal
from agentplane.egress.decisions import Decision
from agentplane.subjects import ServiceAccountRef

logger = logging.getLogger(__name__)

# Seconds without a frame after which the change stream counts as dead: past the proxy's heartbeat.
CHANGES_READ_TIMEOUT_S = 45


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
        self.changes = ChangeSignal()
        self.watching = False

    async def watch(self, *, retry_interval_s: float) -> None:
        """Follows the proxy's change stream until cancelled, waking `changes` at each frame and when the
        stream ends or fails, so a reader learns of a loss as promptly as of a change."""
        while True:
            try:
                async with self._http.stream(
                    "GET", "/decisions/changes", timeout=httpx.Timeout(5, read=CHANGES_READ_TIMEOUT_S)
                ) as response:
                    response.raise_for_status()
                    self.watching = True
                    async for line in response.aiter_lines():
                        if line.startswith("event:"):
                            self.changes.notify()
            except httpx.HTTPError as error:
                logger.warning("egress proxy change stream failed: %r", error)
            finally:
                self.watching = False
                self.changes.notify()
            await asyncio.sleep(retry_interval_s)

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
