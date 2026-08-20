"""The `SandboxClaims` surface implemented in memory, for tests that are not about Kubernetes.

Its own `testonly` module rather than `conftest.py` so that a `testonly` binary can depend on it
too: a `py_binary` cannot reasonably import a `conftest`, and a stand-in that is reachable only
from pytest is one every other process has to reimplement.

The other direction — the real `KubernetesSandboxClaims` with recorded Kubernetes API clients
underneath it — lives in `x/test_sandbox_claims.py`, and is a different job: this one stands in
for the claim builder, that one puts it under test. It stays beside the module it tests rather
than moving here, because it is a test, not a test implementation.

**`inspect` is where this can lie**, and `fixed_provisioning_view` is that lie told once: the
least committal step there is, `CLAIM_CREATED`, which the real implementation gives only when it
created a claim and could not observe past it. A test that is about provisioning state says what
it wants instead (`RecordingClaims.answer`/`fail`); one that is about anything else gets a
well-shaped view it has no reason to read. `fixed_provisioning_view` is shared rather than copied
so that the stand-in in `channels/matrix/testing/console_replica.py` cannot lie differently, and a
test about the *real* step derivation needs `../test_sandbox_claims.py`, not this.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from uuid import UUID

from haku.console.x.sandbox_claims import ProvisioningStep, SandboxProvisioningView, provisioning_view


def fixed_provisioning_view(session_id: UUID) -> SandboxProvisioningView:
    """What every claim stand-in here answers `inspect` with — see the module docstring's caveat."""
    return provisioning_view(f"claude-{session_id.hex}", step=ProvisioningStep.CLAIM_CREATED)


class RecordingClaims:
    """The `SandboxClaims` surface, recording instead of reaching Kubernetes."""

    def __init__(self) -> None:
        self.created: list[UUID] = []
        self.created_event = asyncio.Event()
        self.deleted: list[UUID] = []
        self.renewed: list[tuple[UUID, datetime]] = []
        self.tokens: dict[UUID, str] = {}
        self.refused: set[UUID] = set()
        # Every `inspect`, so a test can assert which reads reached the cluster and which were
        # answered off the cached observation.
        self.inspected: list[UUID] = []
        self._answer: SandboxProvisioningView | None = None
        self._failure: Exception | None = None

    def refuse(self, session_id: UUID) -> None:
        """Make claim creation fail for one session without affecting the rest of a sweep."""
        self.refused.add(session_id)

    def answer(self, view: SandboxProvisioningView) -> None:
        """Make the next inspections report *view* instead of the fixed one."""
        self._answer = view
        self._failure = None

    def fail(self, error: Exception) -> None:
        """Make inspection raise, as an unreachable Kubernetes does."""
        self._failure = error

    async def create(self, *, session_id: UUID, bridge_token: str, expires_at: datetime) -> None:
        assert expires_at > datetime.now(expires_at.tzinfo)
        if session_id in self.refused:
            raise RuntimeError("claim creation refused")
        self.created.append(session_id)
        self.created_event.set()
        # The claim is where a test reaches the credential: the store mints it and
        # `SessionService.create` does not hand them back.
        self.tokens[session_id] = bridge_token

    async def renew(self, *, session_id: UUID, expires_at: datetime) -> None:
        self.renewed.append((session_id, expires_at))

    async def delete(self, *, session_id: UUID) -> None:
        self.deleted.append(session_id)

    async def inspect(self, *, session_id: UUID) -> SandboxProvisioningView:
        self.inspected.append(session_id)
        if self._failure is not None:
            raise self._failure
        return self._answer if self._answer is not None else fixed_provisioning_view(session_id)

    def observation_error(self, *, session_id: UUID, error: str) -> SandboxProvisioningView:
        return provisioning_view(
            f"claude-{session_id.hex}", step=ProvisioningStep.CLAIM_CREATED, observation_error=error
        )

    async def aclose(self) -> None:
        return None
