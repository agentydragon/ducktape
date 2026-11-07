"""Owner-facing session management for configured integration instances."""

from __future__ import annotations

import asyncio
import datetime
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

import httpx2
from starlette.concurrency import run_in_threadpool

from platform_sessions.host import IntegrationError
from platform_sessions.probe import probe_session
from platform_sessions.registry import IntegrationInstance
from platform_sessions.session_store import ProviderSessionStore


@dataclass
class IntegrationRuntime[T](ABC):
    config: IntegrationInstance
    store: ProviderSessionStore[T]
    instructions: Any = None
    periodic: bool = True
    candidate_verification: bool = True
    observation: dict = field(default_factory=lambda: {"outcome": "not_checked"})
    observation_generation: int | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def refresh(self, transport=None, *, source="verify"):
        async with self.lock:
            try:
                before = await run_in_threadpool(self.store.snapshot)
                observation = await run_in_threadpool(
                    probe_session,
                    self.store,
                    transport=transport,
                    source=source,
                    checker=self.verify,
                    provider=self.config.type,
                    instance_id=self.config.id,
                )
                after = await run_in_threadpool(self.store.snapshot)
            except OSError, ValueError:
                self.observation = {"outcome": "storage_error"}
                try:
                    self.observation_generation = self.store.path.stat().st_mtime_ns
                except OSError:
                    self.observation_generation = None
                return
            if before != after:
                self.observation = {"outcome": "superseded"}
                self.observation_generation = None
            else:
                self.observation = observation
                self.observation_generation = after[1]

    async def status(self):
        try:
            generation = self.store.path.stat().st_mtime_ns if self.store.path.exists() else None
        except OSError:
            return {"state": "storage_error", "outcome": "storage_error", "details": []}
        value = self.observation if generation == self.observation_generation else {"outcome": "not_checked"}
        outcome = value.get("outcome", "not_checked")
        if generation is None and outcome == "not_checked":
            outcome = "uninitialized"
        return {
            "state": {
                "success": "ready",
                "network_error": "upstream_unavailable",
                "upstream_http_error": "upstream_unavailable",
                "invalid_response": "upstream_unavailable",
                "superseded": "not_checked",
            }.get(outcome, outcome),
            "outcome": outcome,
            "details": [
                {"key": "imported_at", "value": value.get("imported_at"), "format": "time"},
                {"key": "age_seconds", "value": value.get("age_seconds"), "format": "duration"},
                {"key": "checked_at", "value": value.get("checked_at"), "format": "time"},
                {"key": "outcome", "value": outcome},
            ],
        }

    async def metadata(self):
        return {
            "id": self.config.id,
            "type": self.config.type,
            "instructions": self.instructions,
            "candidate_verification": self.candidate_verification,
        }

    @abstractmethod
    def parse(self, body: bytes) -> T:
        """Decode a provider's session transfer."""

    @abstractmethod
    def verify(self, candidate: T, *, transport=None) -> None:
        """Check a session against the provider without installing it."""

    async def check_candidate(self, candidate, transport=None):
        try:
            await run_in_threadpool(self.verify, candidate, transport=transport)
        except httpx2.HTTPStatusError as exc:
            code = "reauth_required" if exc.response.status_code == 401 else "upstream_rejected"
            raise IntegrationError(code, 400) from None
        except httpx2.ConnectError, httpx2.ConnectTimeout:
            raise IntegrationError("upstream_unavailable", 503) from None
        except httpx2.HTTPError:
            raise IntegrationError("outcome_unknown", 503) from None
        except ValueError, TypeError:
            raise IntegrationError("invalid_response", 502) from None

    async def install(self, candidate, transport=None):
        async with self.lock:
            try:
                await run_in_threadpool(self.verify, candidate, transport=transport)
                await run_in_threadpool(self.store.save, candidate)
            except httpx2.HTTPError, ValueError, TypeError:
                raise IntegrationError("verification_failed", 400) from None
            except OSError:
                raise IntegrationError("outcome_unknown", 503) from None
            now = datetime.datetime.now(datetime.UTC).isoformat()
            self.observation = {"outcome": "success", "checked_at": now, "imported_at": now}
            self.observation_generation = self.store.path.stat().st_mtime_ns
        return {"configured": True}
