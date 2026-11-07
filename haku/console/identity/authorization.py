"""PostgreSQL application service for Haku Agent management and retained OAuth cleanup."""

from __future__ import annotations

import asyncio
import datetime
import errno
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Collection
from contextlib import asynccontextmanager
from typing import TypeVar
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.exc import DBAPIError, InterfaceError, OperationalError, TimeoutError as SQLAlchemyTimeoutError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from haku.console.database_schema import (
    Agent,
    AgentNameReservation,
    AuthorizationGrant,
    ClientSoftware,
    CredentialBinding,
    EnrollmentInteraction,
    Operator,
)
from haku.console.identity.agent import AgentStatus, CredentialBindingStatus, CredentialKind, EnrollmentPhase
from haku.console.identity.agent_management import (
    AccessProfileUnavailableError,
    AgentAccessProfileManagedByDeploymentError,
    AgentNotFoundError,
    OperatorAgent,
)

_ACTIVATION_LIFETIME = datetime.timedelta(minutes=15)
_EXPIRY_SWEEP_INTERVAL = datetime.timedelta(minutes=1)
_EXPIRY_SWEEP_BATCH_SIZE = 100

# asyncpg can surface a raw socket failure before SQLAlchemy has a DBAPIError to wrap it
# in. These are the transport-level errnos that mean "the database is unreachable", not
# an application-level OSError subclass (e.g. AgentAccessProfileManagedByDeploymentError
# is a PermissionError, itself an OSError).
_TRANSIENT_SOCKET_ERRNOS = {
    errno.ECONNREFUSED,
    errno.ECONNRESET,
    errno.ECONNABORTED,
    errno.EPIPE,
    errno.EHOSTUNREACH,
    errno.EHOSTDOWN,
    errno.ENETUNREACH,
    errno.ENETDOWN,
    errno.ETIMEDOUT,
}

_T = TypeVar("_T")

logger = logging.getLogger(__name__)


class AgentGrantAuthorityUnavailableError(Exception):
    """Haku cannot currently reach the Agent authority database."""


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


def _database_is_unavailable(error: DBAPIError) -> bool:
    if isinstance(error, (OperationalError, InterfaceError)) or error.connection_invalidated:
        return True
    sqlstate = getattr(error.orig, "sqlstate", None)
    return isinstance(sqlstate, str) and sqlstate.startswith("08")


class PostgresAgentAuthority:
    """Own operator Agent management and expiry maintenance for retained OAuth history.

    Construction is side-effect free; startup owns the database migration. Synchronous
    SQLAlchemy work runs in a worker thread so FastAPI's event loop is never blocked on PostgreSQL.
    """

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        *,
        clock: Callable[[], datetime.datetime] = _utcnow,
        activation_lifetime: datetime.timedelta = _ACTIVATION_LIFETIME,
        access_profiles: Collection[str],
    ) -> None:
        if activation_lifetime <= datetime.timedelta():
            raise ValueError("Agent authority lifetimes must be positive")
        # The engine/sessionmaker is created once in create_app and shared across every store.
        self._sessions = sessions
        self._clock = clock
        self._activation_lifetime = activation_lifetime
        self._access_profiles = tuple(dict.fromkeys(access_profiles))
        if not self._access_profiles:
            raise ValueError("at least one access profile must be configured")

    def available_access_profiles(self) -> tuple[str, ...]:
        return self._access_profiles

    async def _require_access_profile_id(self, access_profile_id: str) -> None:
        if access_profile_id not in self._access_profiles:
            raise AccessProfileUnavailableError(f"Unknown access profile: {access_profile_id}")

    async def _database_call(self, operation: Callable[[], Awaitable[_T]]) -> _T:
        try:
            return await operation()
        except SQLAlchemyTimeoutError as error:
            raise AgentGrantAuthorityUnavailableError from error
        except OSError as error:
            if error.errno not in _TRANSIENT_SOCKET_ERRNOS:
                raise
            raise AgentGrantAuthorityUnavailableError from error
        except DBAPIError as error:
            if _database_is_unavailable(error):
                raise AgentGrantAuthorityUnavailableError from error
            raise

    async def _now(self) -> datetime.datetime:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("Agent authority clock must return a timezone-aware datetime")
        return now

    async def list_agents(self, *, operator_id: UUID) -> tuple[OperatorAgent, ...]:
        return await self._database_call(lambda: self._list_agents(operator_id))

    async def _list_agents(self, operator_id: UUID) -> tuple[OperatorAgent, ...]:
        async with self._sessions() as session:
            rows = (
                await session.execute(
                    select(Agent, AgentNameReservation.display_name, CredentialBinding)
                    .join(
                        AgentNameReservation, AgentNameReservation.reservation_id == Agent.current_name_reservation_id
                    )
                    .join(CredentialBinding, CredentialBinding.agent_id == Agent.agent_id)
                    .where(
                        Agent.owner_operator_id == operator_id,
                        Agent.status.in_((AgentStatus.ACTIVE, AgentStatus.DISABLED)),
                    )
                    .order_by(AgentNameReservation.display_name_key, CredentialBinding.generation.desc())
                )
            ).all()

        agents: list[OperatorAgent] = []
        seen: set[UUID] = set()
        for agent, display_name, binding in rows:
            if agent.agent_id in seen:
                continue
            seen.add(agent.agent_id)
            agents.append(
                OperatorAgent(
                    agent_id=agent.agent_id,
                    display_name=display_name,
                    status=agent.status,
                    credential_kind=binding.kind,
                    credential_status=binding.status,
                    created_at=agent.created_at,
                    activated_at=agent.activated_at,
                    last_seen_at=agent.last_seen_at,
                    access_profile_id=agent.access_profile_id,
                )
            )
        return tuple(agents)

    async def set_access_profile(self, *, operator_id: UUID, agent_id: UUID, access_profile_id: str) -> OperatorAgent:
        await self._require_access_profile_id(access_profile_id)
        await self._database_call(lambda: self._set_access_profile(operator_id, agent_id, access_profile_id))
        agents = await self.list_agents(operator_id=operator_id)
        for agent in agents:
            if agent.agent_id == agent_id:
                return agent
        raise AgentNotFoundError

    async def _set_access_profile(self, operator_id: UUID, agent_id: UUID, access_profile_id: str) -> None:
        async with self._sessions.begin() as session:
            agent = await session.get(Agent, agent_id, with_for_update=True)
            if (
                agent is None
                or agent.owner_operator_id != operator_id
                or agent.status not in {AgentStatus.ACTIVE, AgentStatus.DISABLED}
            ):
                raise AgentNotFoundError
            latest_binding = await session.scalar(
                select(CredentialBinding)
                .where(CredentialBinding.agent_id == agent_id)
                .order_by(CredentialBinding.generation.desc())
                .limit(1)
                .with_for_update()
            )
            if latest_binding is None:
                raise AgentNotFoundError
            if latest_binding.kind is CredentialKind.STATIC:
                raise AgentAccessProfileManagedByDeploymentError
            agent.access_profile_id = access_profile_id
            agent.updated_at = await self._now()

    async def sweep_expired_state(self, *, batch_size: int = _EXPIRY_SWEEP_BATCH_SIZE) -> int:
        """Expire one unlocked batch of stale enrollment and activation state."""

        if batch_size <= 0:
            raise ValueError("expiry sweep batch size must be positive")
        return await self._database_call(lambda: self._sweep_expired_state(batch_size))

    @asynccontextmanager
    async def expiry_maintenance(
        self, *, interval: datetime.timedelta = _EXPIRY_SWEEP_INTERVAL, batch_size: int = _EXPIRY_SWEEP_BATCH_SIZE
    ) -> AsyncIterator[None]:
        """Sweep before serving, then periodically for this application lifetime."""

        if interval <= datetime.timedelta():
            raise ValueError("expiry sweep interval must be positive")
        await self._drain_expired_state(batch_size)
        stop = asyncio.Event()
        async with asyncio.TaskGroup() as tasks:
            tasks.create_task(
                self._run_expiry_maintenance(stop, interval=interval, batch_size=batch_size),
                name="haku-agent-authority-expiry",
            )
            try:
                yield
            finally:
                stop.set()

    async def _run_expiry_maintenance(
        self, stop: asyncio.Event, *, interval: datetime.timedelta, batch_size: int
    ) -> None:
        while True:
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval.total_seconds())
                return
            except TimeoutError:
                try:
                    await self._drain_expired_state(batch_size)
                except AgentGrantAuthorityUnavailableError:
                    logger.warning("Agent authority expiry sweep unavailable; retrying", exc_info=True)

    async def _drain_expired_state(self, batch_size: int) -> None:
        while await self.sweep_expired_state(batch_size=batch_size) == batch_size:
            pass

    async def _sweep_expired_state(self, batch_size: int) -> int:
        now = await self._now()
        processed = 0
        nonterminal_phases = {
            EnrollmentPhase.AWAITING_BROWSER,
            EnrollmentPhase.AWAITING_APPROVAL,
            EnrollmentPhase.ALLOWED,
            EnrollmentPhase.EXCHANGING,
        }
        async with self._sessions.begin() as session:
            interactions = (
                await session.scalars(
                    select(EnrollmentInteraction)
                    .where(EnrollmentInteraction.phase.in_(nonterminal_phases), EnrollmentInteraction.expires_at <= now)
                    .order_by(EnrollmentInteraction.expires_at, EnrollmentInteraction.interaction_id)
                    .limit(batch_size)
                    .with_for_update(skip_locked=True)
                )
            ).all()
            for interaction in interactions:
                await self._expire_interaction(session, interaction, now, "expiry_sweep")
            processed += len(interactions)

            remaining = batch_size - processed
            if remaining > 0:
                issued_interaction_ids = (
                    select(AuthorizationGrant.enrollment_interaction_id)
                    .join(CredentialBinding, CredentialBinding.binding_id == AuthorizationGrant.binding_id)
                    .where(
                        CredentialBinding.status == CredentialBindingStatus.ISSUED,
                        AuthorizationGrant.token_family_persisted_at.is_not(None),
                        AuthorizationGrant.token_family_persisted_at <= now - self._activation_lifetime,
                    )
                )
                issued_interactions = (
                    await session.scalars(
                        select(EnrollmentInteraction)
                        .where(
                            EnrollmentInteraction.phase == EnrollmentPhase.COMPLETED,
                            EnrollmentInteraction.interaction_id.in_(issued_interaction_ids),
                        )
                        .order_by(EnrollmentInteraction.closed_at, EnrollmentInteraction.interaction_id)
                        .limit(remaining)
                        .with_for_update(skip_locked=True)
                    )
                ).all()
                for interaction in issued_interactions:
                    grant_id = await session.scalar(
                        select(AuthorizationGrant.grant_id).where(
                            AuthorizationGrant.enrollment_interaction_id == interaction.interaction_id
                        )
                    )
                    if grant_id is None:
                        continue
                    rows = await self._locked_grant_rows(session, grant_id)
                    if rows is None:
                        continue
                    grant, binding, agent, _operator, _client = rows
                    if self._activation_expired(grant, binding, now):
                        self._expire_unactivated_binding(binding, agent, now)
                processed += len(issued_interactions)
        return processed

    @staticmethod
    async def _expire_interaction(
        session: AsyncSession, interaction: EnrollmentInteraction, now: datetime.datetime, reason: str
    ) -> None:
        if interaction.phase in {
            EnrollmentPhase.COMPLETED,
            EnrollmentPhase.DENIED,
            EnrollmentPhase.EXPIRED,
            EnrollmentPhase.FAILED,
        }:
            return
        if interaction.phase is EnrollmentPhase.ALLOWED:
            await session.execute(
                delete(AgentNameReservation).where(
                    AgentNameReservation.pending_interaction_id == interaction.interaction_id
                )
            )
        if interaction.phase is EnrollmentPhase.EXCHANGING:
            grant = await session.scalar(
                select(AuthorizationGrant).where(
                    AuthorizationGrant.enrollment_interaction_id == interaction.interaction_id
                )
            )
            if grant is not None:
                binding = await session.get(CredentialBinding, grant.binding_id, with_for_update=True)
                if binding is not None and binding.status is CredentialBindingStatus.ISSUING:
                    binding.status = CredentialBindingStatus.FAILED
                    binding.ended_at = now
                    binding.end_reason = reason
                    binding.updated_at = now
                    if binding.supersedes_binding_id is None:
                        agent = await session.get(Agent, binding.agent_id, with_for_update=True)
                        if agent is not None and agent.status is AgentStatus.DRAFT:
                            agent.status = AgentStatus.ABANDONED
                            agent.updated_at = now
        interaction.phase = EnrollmentPhase.EXPIRED
        interaction.browser_nonce_digest = None
        interaction.browser_binding_digest = None
        interaction.closed_at = now
        interaction.closure_reason = reason
        interaction.updated_at = now

    async def _locked_grant_rows(
        self, session: AsyncSession, grant_id: UUID
    ) -> tuple[AuthorizationGrant, CredentialBinding, Agent, Operator, ClientSoftware] | None:
        row = (
            await session.execute(
                select(AuthorizationGrant, CredentialBinding, Agent, Operator, ClientSoftware)
                .join(CredentialBinding, CredentialBinding.binding_id == AuthorizationGrant.binding_id)
                .join(Agent, Agent.agent_id == CredentialBinding.agent_id)
                .join(Operator, Operator.operator_id == Agent.owner_operator_id)
                .join(ClientSoftware, ClientSoftware.client_software_id == AuthorizationGrant.client_software_id)
                .where(AuthorizationGrant.grant_id == grant_id)
                .with_for_update()
            )
        ).one_or_none()
        if row is None:
            return None
        return row[0], row[1], row[2], row[3], row[4]

    def _activation_expired(
        self, grant: AuthorizationGrant, binding: CredentialBinding, now: datetime.datetime
    ) -> bool:
        return (
            binding.status is CredentialBindingStatus.ISSUED
            and grant.token_family_persisted_at is not None
            and now >= grant.token_family_persisted_at + self._activation_lifetime
        )

    @staticmethod
    def _expire_unactivated_binding(binding: CredentialBinding, agent: Agent, now: datetime.datetime) -> None:
        binding.status = CredentialBindingStatus.EXPIRED
        binding.ended_at = now
        binding.end_reason = "activation_timeout"
        binding.updated_at = now
        if binding.supersedes_binding_id is None:
            agent.status = AgentStatus.ABANDONED
            agent.updated_at = now
