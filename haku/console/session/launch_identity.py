"""Neutral launch identity types shared by channel and runtime stores."""

from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from haku.console.harnesses.kind import HarnessKind
from haku.console.x.runtime import HarnessKey


@dataclass(frozen=True, slots=True)
class LaunchIdentity:
    """The identity selected for a newly-created conversation or its replacement."""

    agent_id: UUID
    binding_id: UUID
    access_profile_id: str
    harness_kind: HarnessKind


class LaunchAgentRejectedError(Exception):
    """A chat launch selected an Agent that is not durably authorized."""


class LaunchAuthorization(Protocol):
    @property
    def agent_id(self) -> UUID: ...

    @property
    def binding_id(self) -> UUID: ...

    @property
    def access_profile_id(self) -> str | None: ...


class LaunchAuthority(Protocol):
    async def launch_authorization(
        self,
        db: AsyncSession,
        *,
        operator_id: UUID,
        agent_id: UUID,
        access_profile_id: str | None = None,
        binding_id: UUID | None = None,
    ) -> LaunchAuthorization: ...


class ChatLaunchAuthorizer:
    """Compose deploy-time runtime gates with the durable Agent authority.

    The authority supplies the current profile for a new launch, or validates the pinned profile
    for a replacement.  Keeping the composition here makes the exact production callable usable
    by channel-neutral tests without importing the application composition root.
    """

    def __init__(
        self,
        authority: LaunchAuthority,
        *,
        launchable_agent_ids: Collection[UUID],
        registered_harness_identities: Collection[HarnessKey],
        profile_harness_kinds: Mapping[str, Collection[HarnessKind]],
    ) -> None:
        self._authority = authority
        self._launchable_agent_ids = frozenset(launchable_agent_ids)
        self._registered_harness_identities = frozenset(registered_harness_identities)
        self._profile_harness_kinds = {
            profile_id: frozenset(harness_kinds) for profile_id, harness_kinds in profile_harness_kinds.items()
        }

    async def __call__(
        self,
        db: AsyncSession,
        operator_id: UUID,
        agent_id: UUID,
        harness_kind: HarnessKind,
        *,
        expected_profile_id: str | None = None,
    ) -> LaunchIdentity:
        if agent_id not in self._launchable_agent_ids:
            raise LaunchAgentRejectedError("selected Agent is not launchable")
        if HarnessKey(agent_id, harness_kind) not in self._registered_harness_identities:
            raise LaunchAgentRejectedError("selected Agent/harness pair is not registered")
        authorization = await self._authority.launch_authorization(
            db, operator_id=operator_id, agent_id=agent_id, access_profile_id=expected_profile_id
        )
        profile_id = authorization.access_profile_id
        if profile_id is None or profile_id not in self._profile_harness_kinds:
            raise LaunchAgentRejectedError("selected Agent has no active access profile")
        if harness_kind not in self._profile_harness_kinds[profile_id]:
            raise LaunchAgentRejectedError("selected access profile disallows the chat harness")
        return LaunchIdentity(
            agent_id=authorization.agent_id,
            binding_id=authorization.binding_id,
            access_profile_id=profile_id,
            harness_kind=harness_kind,
        )


class LaunchAuthorizer(Protocol):
    async def __call__(
        self,
        db: AsyncSession,
        operator_id: UUID,
        agent_id: UUID,
        harness_kind: HarnessKind,
        *,
        expected_profile_id: str | None = None,
    ) -> LaunchIdentity: ...
