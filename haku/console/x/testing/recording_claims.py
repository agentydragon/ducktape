"""The `SandboxClaims` surface implemented in memory, for tests that are not about Kubernetes.

Its own `testonly` module rather than `conftest.py` so that a `testonly` binary can depend on it
too: a `py_binary` cannot reasonably import a `conftest`, and a stand-in that is reachable only
from pytest is one every other process has to reimplement.

The other direction — the real `KubernetesSandboxClaims` with recorded Kubernetes API clients
underneath it — lives in `x/test_sandbox_claims.py`, and is a different job: this one stands in
for the claim builder, that one puts it under test. It stays beside the module it tests rather
than moving here, because it is a test, not a test implementation.

**`inspect` is where this can lie.** It answers with one fixed view rather than deriving a step
from what it has recorded, so a caller reading provisioning state gets an answer the real
implementation would only give for a claim that is *not there* (`CLAIM_CREATED` is its 404 case).
Nothing reads the step today; the annotation is the real type so that at least the shape is
checked, but a test about provisioning steps needs `x/test_sandbox_claims.py`, not this.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from haku.console.x.sandbox_claims import ClaudeSandboxProvisioningView, ProvisioningStep, provisioning_view


class RecordingClaims:
    """The `SandboxClaims` surface, recording instead of reaching Kubernetes."""

    def __init__(self) -> None:
        self.created: list[UUID] = []
        self.deleted: list[UUID] = []
        self.renewed: list[tuple[UUID, datetime]] = []
        self.tokens: dict[UUID, str] = {}

    async def create(self, *, session_id: UUID, bridge_token: str, expires_at: datetime) -> None:
        assert expires_at > datetime.now(expires_at.tzinfo)
        self.created.append(session_id)
        # The claim is where a test reaches the bridge credential: the store mints it and
        # `SessionService.create` does not hand it back.
        self.tokens[session_id] = bridge_token

    async def renew(self, *, session_id: UUID, expires_at: datetime) -> None:
        self.renewed.append((session_id, expires_at))

    async def delete(self, *, session_id: UUID) -> None:
        self.deleted.append(session_id)

    async def inspect(self, *, session_id: UUID) -> ClaudeSandboxProvisioningView:
        # A fixed view, so it agrees with the real `inspect` only by luck — see the module docstring.
        return provisioning_view(f"claude-{session_id.hex}", step=ProvisioningStep.CLAIM_CREATED)

    async def aclose(self) -> None:
        return None
