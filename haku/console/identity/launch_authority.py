"""Durable static-agent launch authorization, importable without the MCP auth surface.

The chat-launch half of the Agent authority: pure database reads plus guard locks, no enrollment,
bearer, or FastMCP machinery. Split out of `authorization.py` so a channel worker binary can
authorize conversation opens without its BUILD deps reaching the console's auth stack
(<../docs/naming_and_layout.md> §5). `PostgresAgentAuthority` delegates here for the same query.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from haku.console.database_schema import Agent, CredentialBinding, Operator, StaticCredential
from haku.console.identity.agent import AgentStatus, CredentialBindingStatus, CredentialKind
from haku.console.identity.operator_identity import OperatorStatus
from haku.console.session.launch_identity import LaunchAgentRejectedError


@dataclass(frozen=True, slots=True)
class StaticAgentAuthorization:
    """Canonical runtime identity of one currently authorized static binding."""

    agent_id: UUID
    binding_id: UUID
    operator_id: UUID
    access_profile_id: str | None = None


class StaticLaunchAuthority:
    """The durable-authority arm of `x/launch_identity.LaunchAuthority`, stateless."""

    async def launch_authorization(
        self,
        db: AsyncSession,
        *,
        operator_id: UUID,
        agent_id: UUID,
        access_profile_id: str | None = None,
        binding_id: UUID | None = None,
    ) -> StaticAgentAuthorization:
        """Resolve active launch authority at the caller's transaction linearization point."""
        if not db.in_transaction():
            raise RuntimeError("launch authorization requires an active caller transaction")
        # Lock in a stable order (operators → agents → credential_bindings → static_credentials).
        # The locks are held until the caller's transaction commits, so a concurrent
        # disable/rotation cannot pass this check and then invalidate the conversation or
        # attachment before it is durable.
        #
        # Guard locks, so FOR NO KEY UPDATE — never FOR UPDATE. A disable/rotation is a non-key
        # UPDATE, which FOR NO KEY UPDATE already blocks. FOR UPDATE would additionally conflict
        # with the implicit FOR KEY SHARE that any concurrent INSERT/UPDATE referencing these rows
        # takes through its foreign-key check (a session or conversation write naming its
        # operator). Such a writer already holds its own rows, so the stronger mode closes a lock
        # cycle and deadlocks where this one merely waits its turn.
        operator = await db.get(Operator, operator_id, with_for_update={"key_share": True})
        agent = await db.get(Agent, agent_id, with_for_update={"key_share": True})
        binding = await db.scalar(
            select(CredentialBinding)
            .where(
                CredentialBinding.agent_id == agent_id,
                CredentialBinding.kind == CredentialKind.STATIC,
                CredentialBinding.status == CredentialBindingStatus.ACTIVE,
                *((CredentialBinding.binding_id == binding_id,) if binding_id is not None else ()),
            )
            .order_by(CredentialBinding.activated_at.desc())
            .limit(1)
            .with_for_update(key_share=True)
        )
        credential = (
            None
            if binding is None
            else await db.get(StaticCredential, binding.binding_id, with_for_update={"key_share": True})
        )
        if (
            operator is None
            or agent is None
            or credential is None
            or operator.status is not OperatorStatus.ACTIVE
            or agent.owner_operator_id != operator_id
            or agent.status is not AgentStatus.ACTIVE
            or binding is None
            or binding.kind is not CredentialKind.STATIC
            or binding.status is not CredentialBindingStatus.ACTIVE
        ):
            raise LaunchAgentRejectedError
        # Replacement sessions use the conversation's pinned profile.  The Agent's current profile
        # is deliberately not required to equal it: profile changes must not retarget an old thread.
        return StaticAgentAuthorization(
            agent.agent_id,
            binding.binding_id,
            operator.operator_id,
            access_profile_id if access_profile_id is not None else agent.access_profile_id,
        )
