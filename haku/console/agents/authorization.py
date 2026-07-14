"""PostgreSQL application service for Haku Agent enrollment and OAuth grants."""

from __future__ import annotations

import asyncio
import datetime
import hashlib
import hmac
import json
import logging
import secrets
from collections.abc import AsyncIterator, Callable, Collection
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TypeVar
from urllib.parse import urlencode, urlsplit
from uuid import UUID, uuid4

from sqlalchemy import create_engine, delete, func, select, text
from sqlalchemy.exc import DBAPIError, InterfaceError, OperationalError, TimeoutError as SQLAlchemyTimeoutError
from sqlalchemy.orm import Session, sessionmaker

from haku.console.agents.enrollment import (
    AgentNameUnavailableError,
    CreateAgentDecision,
    DenyEnrollmentDecision,
    EnrollmentAllowed,
    EnrollmentBrowserBindingError,
    EnrollmentBrowserSession,
    EnrollmentDecision,
    EnrollmentDecisionConflictError,
    EnrollmentDecisionResult,
    EnrollmentDenied,
    EnrollmentInteractionExpiredError,
    EnrollmentInteractionNotFoundError,
    EnrollmentPage,
    ReconnectableAgent,
    ReconnectAgentDecision,
)
from haku.console.agents.models import (
    AgentStatus,
    ClientRegistrationKind,
    CredentialBindingStatus,
    CredentialKind,
    EnrollmentPhase,
)
from haku.console.agents.naming import InvalidAgentNameError, NormalizedAgentName, normalize_agent_name
from haku.console.database_schema import (
    Agent,
    AgentNameReservation,
    AuthorizationGrant,
    ClientSoftware,
    CredentialBinding,
    EnrollmentCorrelationReservation,
    EnrollmentInteraction,
    IdentityAnchor,
    OidcIdentity,
    Operator,
    StaticCredential,
)
from haku.console.mcp_auth.fastmcp_adapter import (
    AgentGrantAuthorityUnavailableError,
    AuthorizationCorrelation,
    AuthorizationRequest,
    ClientSoftwareSnapshot,
    DuplicateAuthorizationError,
    EnrollmentRejectedError,
    ExchangeAlreadyClaimedError,
    GrantAuthorization,
    GrantRejectedError,
    TokenFamilyEvidence,
)
from haku.console.operator_identity import (
    InactiveOperatorError,
    OperatorIdentityError,
    OperatorStatus,
    VerifiedExternalIdentity,
)
from haku.console.operator_identity_store import PostgresOperatorIdentityStore
from haku.console.tool_call_actor import AgentActor
from mcp_infra.authentik_auth.oidc_principal import VerifiedOidcPrincipal

_INTERACTION_LIFETIME = datetime.timedelta(minutes=10)
_CORRELATION_RETENTION = datetime.timedelta(hours=2)
_ACTIVATION_LIFETIME = datetime.timedelta(minutes=15)
_EXPIRY_SWEEP_INTERVAL = datetime.timedelta(minutes=1)
_EXPIRY_SWEEP_BATCH_SIZE = 100
_BROWSER_SECRET_BYTES = 32

_T = TypeVar("_T")

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class StaticAgentDefinition:
    """Controller-owned identity and credential evidence for one static Agent slot."""

    agent_id: UUID
    display_name: str
    operator_id: UUID
    secret_reference: str
    token_fingerprint: bytes


@dataclass(frozen=True, slots=True)
class StaticAgentAuthorization:
    """Canonical runtime identity of one currently authorized static binding."""

    agent_id: UUID
    binding_id: UUID
    operator_id: UUID


class StaticAgentDefinitionError(ValueError):
    """Static Agent configuration contradicts durable authority state."""


class StaticAgentRejectedError(Exception):
    """A static binding or fingerprint is not currently authorized."""


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


def _new_browser_secret() -> str:
    return secrets.token_urlsafe(_BROWSER_SECRET_BYTES)


def fingerprint_static_token(token: str) -> bytes:
    """Hash a configured bearer without retaining the raw credential."""

    if not token:
        raise ValueError("static Agent token must not be empty")
    return hashlib.sha256(token.encode("utf-8")).digest()


def _digest(*parts: str) -> bytes:
    digest = hashlib.sha256()
    for part in parts:
        encoded = part.encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return digest.digest()


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True)


def _database_is_unavailable(error: DBAPIError) -> bool:
    if isinstance(error, (OperationalError, InterfaceError)) or error.connection_invalidated:
        return True
    sqlstate = getattr(error.orig, "sqlstate", None)
    return isinstance(sqlstate, str) and sqlstate.startswith("08")


class PostgresAgentAuthority:
    """Own the one transactional Agent/name/binding/grant aggregate.

    Construction is side-effect free; startup owns migration 0009. Synchronous
    SQLAlchemy work runs in a worker thread so FastMCP and FastAPI event loops
    are never blocked on PostgreSQL.
    """

    def __init__(
        self,
        database_url: str,
        *,
        public_base_url: str,
        operator_identity_store: PostgresOperatorIdentityStore,
        clock: Callable[[], datetime.datetime] = _utcnow,
        browser_secret_factory: Callable[[], str] = _new_browser_secret,
        interaction_lifetime: datetime.timedelta = _INTERACTION_LIFETIME,
        correlation_retention: datetime.timedelta = _CORRELATION_RETENTION,
        activation_lifetime: datetime.timedelta = _ACTIVATION_LIFETIME,
    ) -> None:
        parsed_base = urlsplit(public_base_url)
        if (
            parsed_base.scheme not in {"http", "https"}
            or not parsed_base.netloc
            or parsed_base.query
            or parsed_base.fragment
        ):
            raise ValueError("public_base_url must be an absolute HTTP(S) URL without query or fragment")
        if min(interaction_lifetime, correlation_retention, activation_lifetime) <= datetime.timedelta():
            raise ValueError("Agent authority lifetimes must be positive")
        self._engine = create_engine(database_url, pool_pre_ping=True)
        self._sessions = sessionmaker(self._engine, expire_on_commit=False)
        self._operator_identities = operator_identity_store
        self._public_base_url = public_base_url.rstrip("/")
        self._clock = clock
        self._browser_secret_factory = browser_secret_factory
        self._interaction_lifetime = interaction_lifetime
        self._correlation_retention = correlation_retention
        self._activation_lifetime = activation_lifetime

    async def _database_call(self, operation: Callable[[], _T]) -> _T:
        try:
            return await asyncio.to_thread(operation)
        except SQLAlchemyTimeoutError as error:
            raise AgentGrantAuthorityUnavailableError from error
        except DBAPIError as error:
            if _database_is_unavailable(error):
                raise AgentGrantAuthorityUnavailableError from error
            raise

    def _now(self) -> datetime.datetime:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("Agent authority clock must return a timezone-aware datetime")
        return now

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

    def _sweep_expired_state(self, batch_size: int) -> int:
        now = self._now()
        processed = 0
        nonterminal_phases = {
            EnrollmentPhase.AWAITING_BROWSER,
            EnrollmentPhase.AWAITING_APPROVAL,
            EnrollmentPhase.ALLOWED,
            EnrollmentPhase.EXCHANGING,
        }
        with self._sessions.begin() as session:
            interactions = session.scalars(
                select(EnrollmentInteraction)
                .where(EnrollmentInteraction.phase.in_(nonterminal_phases), EnrollmentInteraction.expires_at <= now)
                .order_by(EnrollmentInteraction.expires_at, EnrollmentInteraction.interaction_id)
                .limit(batch_size)
                .with_for_update(skip_locked=True)
            ).all()
            for interaction in interactions:
                self._expire_interaction(session, interaction, now, "expiry_sweep")
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
                issued_interactions = session.scalars(
                    select(EnrollmentInteraction)
                    .where(
                        EnrollmentInteraction.phase == EnrollmentPhase.COMPLETED,
                        EnrollmentInteraction.interaction_id.in_(issued_interaction_ids),
                    )
                    .order_by(EnrollmentInteraction.closed_at, EnrollmentInteraction.interaction_id)
                    .limit(remaining)
                    .with_for_update(skip_locked=True)
                ).all()
                for interaction in issued_interactions:
                    grant_id = session.scalar(
                        select(AuthorizationGrant.grant_id).where(
                            AuthorizationGrant.enrollment_interaction_id == interaction.interaction_id
                        )
                    )
                    if grant_id is None:
                        continue
                    rows = self._locked_grant_rows(session, grant_id)
                    if rows is None:
                        continue
                    grant, binding, agent, _operator, _client = rows
                    if self._activation_expired(grant, binding, now):
                        self._expire_unactivated_binding(binding, agent, now)
                processed += len(issued_interactions)
        return processed

    async def reconcile_static_agents(
        self, definitions: Collection[StaticAgentDefinition]
    ) -> tuple[StaticAgentAuthorization, ...]:
        """Make this complete desired static-Agent set current atomically."""

        return await self._database_call(lambda: self._reconcile_static_agents(definitions))

    def _reconcile_static_agents(
        self, definitions: Collection[StaticAgentDefinition]
    ) -> tuple[StaticAgentAuthorization, ...]:
        prepared = self._prepare_static_definitions(definitions)
        now = self._now()
        authorizations: list[StaticAgentAuthorization] = []
        with self._sessions.begin() as session:
            self._lock_key(session, "static-agent-reconcile")
            for definition, name in sorted(prepared, key=lambda item: item[0].agent_id.int):
                self._lock_key(session, "static-agent", str(definition.agent_id))
                self._operator_identities.require_active_in_transaction(session, definition.operator_id)
                agent = session.get(Agent, definition.agent_id, with_for_update=True)
                if agent is None:
                    self._lock_key(session, "agent-name", name.reservation_key)
                    self._require_name_available(session, name.reservation_key, agent_id=None)
                    reservation_id = uuid4()
                    agent = Agent(
                        agent_id=definition.agent_id,
                        owner_operator_id=definition.operator_id,
                        current_name_reservation_id=reservation_id,
                        status=AgentStatus.ACTIVE,
                        created_at=now,
                        updated_at=now,
                        activated_at=now,
                        last_seen_at=None,
                    )
                    session.add(agent)
                    session.flush()
                    session.add(
                        AgentNameReservation(
                            reservation_id=reservation_id,
                            display_name=name.display_name,
                            display_name_key=name.reservation_key,
                            originating_interaction_id=None,
                            pending_interaction_id=None,
                            agent_id=agent.agent_id,
                            created_at=now,
                            activated_at=now,
                        )
                    )
                    session.flush()
                else:
                    if agent.owner_operator_id != definition.operator_id or agent.status not in {
                        AgentStatus.ACTIVE,
                        AgentStatus.DISABLED,
                    }:
                        raise StaticAgentDefinitionError(
                            f"static Agent slot {definition.agent_id} conflicts with durable Agent ownership or status"
                        )
                    self._reconcile_static_name(session, agent, name, now)

                active_row = session.execute(
                    select(CredentialBinding, StaticCredential)
                    .join(StaticCredential, StaticCredential.binding_id == CredentialBinding.binding_id)
                    .where(
                        CredentialBinding.agent_id == agent.agent_id,
                        CredentialBinding.status == CredentialBindingStatus.ACTIVE,
                    )
                    .with_for_update()
                ).one_or_none()
                if active_row is not None:
                    active_binding, active_credential = active_row
                    if active_binding.kind is not CredentialKind.STATIC:
                        raise StaticAgentDefinitionError(
                            f"static Agent slot {definition.agent_id} is already owned by a non-static binding"
                        )
                    if hmac.compare_digest(active_credential.credential_fingerprint, definition.token_fingerprint):
                        if active_credential.secret_reference != definition.secret_reference:
                            raise StaticAgentDefinitionError(
                                f"static Agent slot {definition.agent_id} reused a fingerprint from another secret source"
                            )
                        authorizations.append(
                            StaticAgentAuthorization(agent.agent_id, active_binding.binding_id, agent.owner_operator_id)
                        )
                        continue
                    active_binding.status = CredentialBindingStatus.REVOKED
                    active_binding.ended_at = now
                    active_binding.end_reason = "static_credential_rotated"
                    active_binding.updated_at = now
                    predecessor_id = active_binding.binding_id
                else:
                    predecessor_id = None

                fingerprint_owner = session.scalar(
                    select(StaticCredential.binding_id).where(
                        StaticCredential.credential_fingerprint == definition.token_fingerprint
                    )
                )
                if fingerprint_owner is not None:
                    raise StaticAgentDefinitionError("static Agent token fingerprint was already used")
                latest = session.scalar(
                    select(CredentialBinding)
                    .where(CredentialBinding.agent_id == agent.agent_id)
                    .order_by(CredentialBinding.generation.desc())
                    .limit(1)
                    .with_for_update()
                )
                if latest is not None:
                    if latest.kind is not CredentialKind.STATIC:
                        raise StaticAgentDefinitionError("static Agent slot has non-static credential history")
                    if latest.status not in {
                        CredentialBindingStatus.REVOKED,
                        CredentialBindingStatus.EXPIRED,
                        CredentialBindingStatus.FAILED,
                    }:
                        raise StaticAgentDefinitionError("static Agent has a nonterminal non-active binding")
                    if predecessor_id is None:
                        predecessor_id = latest.binding_id
                    generation = latest.generation + 1
                else:
                    generation = 1
                binding_id = uuid4()
                session.add(
                    CredentialBinding(
                        binding_id=binding_id,
                        agent_id=agent.agent_id,
                        kind=CredentialKind.STATIC,
                        status=CredentialBindingStatus.ACTIVE,
                        generation=generation,
                        supersedes_binding_id=predecessor_id,
                        created_at=now,
                        updated_at=now,
                        issued_at=now,
                        activated_at=now,
                        ended_at=None,
                        end_reason=None,
                    )
                )
                session.flush()
                session.add(
                    StaticCredential(
                        binding_id=binding_id,
                        secret_reference=definition.secret_reference,
                        credential_fingerprint=definition.token_fingerprint,
                        created_at=now,
                    )
                )
                authorizations.append(StaticAgentAuthorization(agent.agent_id, binding_id, agent.owner_operator_id))
                if agent.status is AgentStatus.DISABLED:
                    agent.status = AgentStatus.ACTIVE
                    agent.updated_at = now

            desired_agent_ids = tuple(definition.agent_id for definition, _name in prepared)
            removed = session.execute(
                select(Agent, CredentialBinding)
                .join(CredentialBinding, CredentialBinding.agent_id == Agent.agent_id)
                .where(
                    CredentialBinding.kind == CredentialKind.STATIC,
                    CredentialBinding.status == CredentialBindingStatus.ACTIVE,
                    Agent.agent_id.not_in(desired_agent_ids),
                )
                .order_by(Agent.agent_id)
                .with_for_update()
            ).all()
            for agent, binding in removed:
                binding.status = CredentialBindingStatus.REVOKED
                binding.ended_at = now
                binding.end_reason = "static_configuration_removed"
                binding.updated_at = now
                agent.status = AgentStatus.DISABLED
                agent.updated_at = now
        return tuple(authorizations)

    async def static_authorization_for_binding(self, *, binding_id: UUID) -> StaticAgentAuthorization:
        return await self._database_call(lambda: self._static_authorization(binding_id=binding_id, fingerprint=None))

    async def static_authorization_for_fingerprint(self, *, fingerprint: bytes) -> StaticAgentAuthorization:
        if not fingerprint:
            raise StaticAgentRejectedError
        return await self._database_call(lambda: self._static_authorization(binding_id=None, fingerprint=fingerprint))

    def _static_authorization(self, *, binding_id: UUID | None, fingerprint: bytes | None) -> StaticAgentAuthorization:
        with self._sessions.begin() as session:
            statement = (
                select(Agent, CredentialBinding, StaticCredential, Operator)
                .join(CredentialBinding, CredentialBinding.agent_id == Agent.agent_id)
                .join(StaticCredential, StaticCredential.binding_id == CredentialBinding.binding_id)
                .join(Operator, Operator.operator_id == Agent.owner_operator_id)
            )
            if binding_id is not None:
                statement = statement.where(CredentialBinding.binding_id == binding_id)
            else:
                assert fingerprint is not None
                statement = statement.where(StaticCredential.credential_fingerprint == fingerprint)
            row = session.execute(statement.with_for_update()).one_or_none()
            if row is None:
                raise StaticAgentRejectedError
            agent, binding, _credential, operator = row
            if (
                agent.status is not AgentStatus.ACTIVE
                or binding.kind is not CredentialKind.STATIC
                or binding.status is not CredentialBindingStatus.ACTIVE
                or operator.status is not OperatorStatus.ACTIVE
            ):
                raise StaticAgentRejectedError
            return StaticAgentAuthorization(agent.agent_id, binding.binding_id, operator.operator_id)

    @staticmethod
    def _prepare_static_definitions(
        definitions: Collection[StaticAgentDefinition],
    ) -> list[tuple[StaticAgentDefinition, NormalizedAgentName]]:
        prepared: list[tuple[StaticAgentDefinition, NormalizedAgentName]] = []
        agent_ids: set[UUID] = set()
        name_keys: set[str] = set()
        fingerprints: set[bytes] = set()
        for definition in definitions:
            if (
                definition.agent_id in agent_ids
                or not definition.secret_reference.strip()
                or not isinstance(definition.token_fingerprint, bytes)
                or not definition.token_fingerprint
            ):
                raise StaticAgentDefinitionError("static Agent definitions require unique ids and nonempty evidence")
            try:
                name = normalize_agent_name(definition.display_name)
            except InvalidAgentNameError as error:
                raise StaticAgentDefinitionError(str(error)) from error
            if name.reservation_key in name_keys or definition.token_fingerprint in fingerprints:
                raise StaticAgentDefinitionError("static Agent names and token fingerprints must be globally unique")
            agent_ids.add(definition.agent_id)
            name_keys.add(name.reservation_key)
            fingerprints.add(definition.token_fingerprint)
            prepared.append((definition, name))
        return prepared

    @staticmethod
    def _require_name_available(session: Session, name_key: str, agent_id: UUID | None) -> None:
        reservation = session.scalar(
            select(AgentNameReservation).where(AgentNameReservation.display_name_key == name_key)
        )
        if reservation is not None and reservation.agent_id != agent_id:
            raise StaticAgentDefinitionError("static Agent display name is already reserved")

    def _reconcile_static_name(
        self, session: Session, agent: Agent, name: NormalizedAgentName, now: datetime.datetime
    ) -> None:
        current = session.get(AgentNameReservation, agent.current_name_reservation_id)
        if current is None:
            raise StaticAgentDefinitionError("static Agent current display name is missing")
        if current.display_name_key == name.reservation_key:
            return
        self._lock_key(session, "agent-name", name.reservation_key)
        existing = session.scalar(
            select(AgentNameReservation).where(AgentNameReservation.display_name_key == name.reservation_key)
        )
        if existing is not None:
            if existing.agent_id != agent.agent_id:
                raise StaticAgentDefinitionError("static Agent display name is already reserved")
            agent.current_name_reservation_id = existing.reservation_id
            agent.updated_at = now
            return
        reservation_id = uuid4()
        session.add(
            AgentNameReservation(
                reservation_id=reservation_id,
                display_name=name.display_name,
                display_name_key=name.reservation_key,
                originating_interaction_id=None,
                pending_interaction_id=None,
                agent_id=agent.agent_id,
                created_at=now,
                activated_at=now,
            )
        )
        agent.current_name_reservation_id = reservation_id
        agent.updated_at = now

    async def reserve_authorization(self, *, request: AuthorizationRequest, upstream_authorization_url: str) -> str:
        return await self._database_call(lambda: self._reserve_authorization(request, upstream_authorization_url))

    def _reserve_authorization(self, request: AuthorizationRequest, upstream_authorization_url: str) -> str:
        self._validate_authorization_request(request, upstream_authorization_url)
        interaction_id = uuid4()
        browser_nonce = self._browser_secret_factory()
        if not browser_nonce:
            raise ValueError("browser secret factory returned an empty secret")
        now = self._now()
        expires_at = now + self._interaction_lifetime
        release_after = expires_at + self._correlation_retention
        presentation = {
            "client_id": request.client.client_id,
            "display_name": request.client.display_name,
            "redirect_uris": list(request.client.redirect_uris),
        }
        with self._sessions.begin() as session:
            self._lock_key(session, "agent-client", request.client.client_id)
            self._lock_key(session, "agent-correlation", *self._correlation_parts(request.correlation))
            session.execute(
                delete(EnrollmentCorrelationReservation).where(
                    EnrollmentCorrelationReservation.release_after <= func.clock_timestamp()
                )
            )
            duplicate = session.scalar(
                select(EnrollmentCorrelationReservation.interaction_id).where(
                    EnrollmentCorrelationReservation.client_id == request.correlation.client_id,
                    EnrollmentCorrelationReservation.redirect_uri == request.correlation.redirect_uri,
                    EnrollmentCorrelationReservation.code_challenge == request.correlation.code_challenge,
                )
            )
            if duplicate is not None:
                raise DuplicateAuthorizationError
            client = self._upsert_client(session, request.client, now)
            interaction = EnrollmentInteraction(
                interaction_id=interaction_id,
                client_software_id=client.client_software_id,
                client_id=request.correlation.client_id,
                redirect_uri=request.correlation.redirect_uri,
                code_challenge=request.correlation.code_challenge,
                requested_scopes=sorted(request.requested_scopes),
                presentation_snapshot=presentation,
                upstream_authorization_url=upstream_authorization_url,
                phase=EnrollmentPhase.AWAITING_BROWSER,
                expires_at=expires_at,
                correlation_release_after=release_after,
                browser_nonce_digest=_digest("browser-nonce", browser_nonce),
                browser_identity_id=None,
                browser_binding_digest=None,
                decision_digest=None,
                reconnect_agent_id=None,
                reconnect_predecessor_binding_id=None,
                closure_reason=None,
                created_at=now,
                updated_at=now,
                closed_at=None,
            )
            session.add(interaction)
            session.flush()
            session.add(
                EnrollmentCorrelationReservation(
                    interaction_id=interaction_id,
                    client_id=request.correlation.client_id,
                    redirect_uri=request.correlation.redirect_uri,
                    code_challenge=request.correlation.code_challenge,
                    release_after=release_after,
                )
            )
        query = urlencode({"browser_nonce": browser_nonce})
        return f"{self._public_base_url}/auth/agent-enrollment/{interaction_id}?{query}"

    async def open_interaction(
        self,
        *,
        interaction_id: UUID,
        browser_nonce: str | None,
        interaction_cookie: str | None,
        browser: EnrollmentBrowserSession,
    ) -> EnrollmentPage:
        return await self._database_call(
            lambda: self._open_interaction(interaction_id, browser_nonce, interaction_cookie, browser)
        )

    def _open_interaction(
        self,
        interaction_id: UUID,
        browser_nonce: str | None,
        interaction_cookie: str | None,
        browser: EnrollmentBrowserSession,
    ) -> EnrollmentPage:
        error: Exception | None = None
        page: EnrollmentPage | None = None
        now = self._now()
        with self._sessions.begin() as session:
            interaction = session.get(EnrollmentInteraction, interaction_id, with_for_update=True)
            if interaction is None:
                raise EnrollmentInteractionNotFoundError
            self._require_browser_identity(session, browser)
            if now >= interaction.expires_at:
                self._expire_interaction(session, interaction, now, "browser_interaction_timeout")
                error = EnrollmentInteractionExpiredError()
            elif interaction.phase is EnrollmentPhase.AWAITING_BROWSER:
                if (
                    browser_nonce is None
                    or interaction.browser_nonce_digest is None
                    or not hmac.compare_digest(
                        interaction.browser_nonce_digest, _digest("browser-nonce", browser_nonce)
                    )
                ):
                    raise EnrollmentBrowserBindingError
                form_token = self._browser_secret_factory()
                if not form_token:
                    raise ValueError("browser secret factory returned an empty secret")
                interaction.phase = EnrollmentPhase.AWAITING_APPROVAL
                interaction.browser_nonce_digest = None
                interaction.browser_identity_id = browser.identity_id
                interaction.browser_binding_digest = self._browser_binding_digest(form_token, browser)
                interaction.updated_at = now
                page = self._enrollment_page(session, interaction, browser, form_token)
            elif interaction.phase is EnrollmentPhase.AWAITING_APPROVAL:
                if interaction_cookie is None:
                    raise EnrollmentBrowserBindingError
                self._require_interaction_browser(interaction, browser, interaction_cookie)
                page = self._enrollment_page(session, interaction, browser, interaction_cookie)
            elif interaction.phase is EnrollmentPhase.EXPIRED:
                error = EnrollmentInteractionExpiredError()
            else:
                error = EnrollmentDecisionConflictError()
        if error is not None:
            raise error
        assert page is not None
        return page

    async def decide(
        self,
        *,
        interaction_id: UUID,
        browser: EnrollmentBrowserSession,
        interaction_cookie: str,
        decision: EnrollmentDecision,
    ) -> EnrollmentDecisionResult:
        return await self._database_call(lambda: self._decide(interaction_id, browser, interaction_cookie, decision))

    def _decide(
        self,
        interaction_id: UUID,
        browser: EnrollmentBrowserSession,
        interaction_cookie: str,
        decision: EnrollmentDecision,
    ) -> EnrollmentDecisionResult:
        normalized_name = (
            normalize_agent_name(decision.display_name) if isinstance(decision, CreateAgentDecision) else None
        )
        if isinstance(decision, CreateAgentDecision):
            assert normalized_name is not None
            decision_payload = {"kind": "create", "display_name_key": normalized_name.reservation_key}
        elif isinstance(decision, ReconnectAgentDecision):
            decision_payload = {"kind": "reconnect", "agent_id": str(decision.agent_id)}
        elif isinstance(decision, DenyEnrollmentDecision):
            decision_payload = {"kind": "deny"}
        else:
            raise TypeError(f"unsupported enrollment decision: {type(decision).__name__}")
        proposed_digest = self._decision_digest(interaction_cookie, browser, decision_payload)
        now = self._now()
        error: Exception | None = None
        result: EnrollmentDecisionResult | None = None
        with self._sessions.begin() as session:
            interaction = session.get(EnrollmentInteraction, interaction_id, with_for_update=True)
            if interaction is None:
                raise EnrollmentInteractionNotFoundError
            self._require_browser_identity(session, browser)
            if not hmac.compare_digest(interaction_cookie, decision.form_token):
                raise EnrollmentBrowserBindingError
            if now >= interaction.expires_at:
                self._expire_interaction(session, interaction, now, "operator_decision_timeout")
                error = EnrollmentInteractionExpiredError()
            elif interaction.phase in {EnrollmentPhase.ALLOWED, EnrollmentPhase.DENIED}:
                expected_phase = (
                    EnrollmentPhase.DENIED if isinstance(decision, DenyEnrollmentDecision) else EnrollmentPhase.ALLOWED
                )
                if (
                    interaction.phase is not expected_phase
                    or interaction.browser_identity_id != browser.identity_id
                    or interaction.decision_digest is None
                    or not hmac.compare_digest(interaction.decision_digest, proposed_digest)
                ):
                    error = EnrollmentDecisionConflictError()
                else:
                    result = (
                        EnrollmentDenied()
                        if expected_phase is EnrollmentPhase.DENIED
                        else EnrollmentAllowed(interaction.upstream_authorization_url)
                    )
            elif interaction.phase is not EnrollmentPhase.AWAITING_APPROVAL:
                error = (
                    EnrollmentInteractionExpiredError()
                    if interaction.phase is EnrollmentPhase.EXPIRED
                    else EnrollmentDecisionConflictError()
                )
            else:
                self._require_interaction_browser(interaction, browser, interaction_cookie)
                if isinstance(decision, CreateAgentDecision):
                    assert normalized_name is not None
                    self._lock_key(session, "agent-name", normalized_name.reservation_key)
                    if (
                        session.scalar(
                            select(AgentNameReservation.reservation_id).where(
                                AgentNameReservation.display_name_key == normalized_name.reservation_key
                            )
                        )
                        is not None
                    ):
                        raise AgentNameUnavailableError("Agent name is already reserved.")
                    session.add(
                        AgentNameReservation(
                            reservation_id=uuid4(),
                            display_name=normalized_name.display_name,
                            display_name_key=normalized_name.reservation_key,
                            originating_interaction_id=interaction.interaction_id,
                            pending_interaction_id=interaction.interaction_id,
                            agent_id=None,
                            created_at=now,
                            activated_at=None,
                        )
                    )
                elif isinstance(decision, ReconnectAgentDecision):
                    reconnect = session.execute(
                        select(Agent, CredentialBinding)
                        .join(CredentialBinding, CredentialBinding.agent_id == Agent.agent_id)
                        .where(
                            Agent.agent_id == decision.agent_id,
                            CredentialBinding.status == CredentialBindingStatus.ACTIVE,
                        )
                        .with_for_update()
                    ).one_or_none()
                    if reconnect is None:
                        raise EnrollmentDecisionConflictError
                    agent, binding = reconnect
                    if agent.owner_operator_id != browser.operator_id:
                        raise EnrollmentBrowserBindingError
                    if agent.status is not AgentStatus.ACTIVE or binding.kind is not CredentialKind.OAUTH:
                        raise EnrollmentDecisionConflictError
                    interaction.reconnect_agent_id = agent.agent_id
                    interaction.reconnect_predecessor_binding_id = binding.binding_id
                elif isinstance(decision, DenyEnrollmentDecision):
                    interaction.phase = EnrollmentPhase.DENIED
                    interaction.decision_digest = proposed_digest
                    interaction.browser_binding_digest = None
                    interaction.closed_at = now
                    interaction.closure_reason = "operator_denied"
                    interaction.updated_at = now
                    result = EnrollmentDenied()
                if not isinstance(decision, DenyEnrollmentDecision):
                    interaction.phase = EnrollmentPhase.ALLOWED
                    interaction.decision_digest = proposed_digest
                    interaction.updated_at = now
                    result = EnrollmentAllowed(interaction.upstream_authorization_url)
        if error is not None:
            raise error
        assert result is not None
        return result

    async def begin_exchange(
        self,
        *,
        correlation: AuthorizationCorrelation,
        client: ClientSoftwareSnapshot,
        principal: VerifiedOidcPrincipal,
        granted_scopes: frozenset[str],
    ) -> GrantAuthorization:
        return await self._database_call(lambda: self._begin_exchange(correlation, client, principal, granted_scopes))

    def _begin_exchange(
        self,
        correlation: AuthorizationCorrelation,
        client: ClientSoftwareSnapshot,
        principal: VerifiedOidcPrincipal,
        granted_scopes: frozenset[str],
    ) -> GrantAuthorization:
        try:
            resolved = self._operator_identities.resolve_verified_identity(
                VerifiedExternalIdentity(issuer=principal.issuer, subject=principal.subject)
            )
        except OperatorIdentityError as identity_error:
            raise EnrollmentRejectedError from identity_error
        now = self._now()
        failure: Exception | None = None
        authorization: GrantAuthorization | None = None
        with self._sessions.begin() as session:
            self._lock_key(session, "agent-correlation", *self._correlation_parts(correlation))
            reservation = session.execute(
                select(EnrollmentCorrelationReservation)
                .where(
                    EnrollmentCorrelationReservation.client_id == correlation.client_id,
                    EnrollmentCorrelationReservation.redirect_uri == correlation.redirect_uri,
                    EnrollmentCorrelationReservation.code_challenge == correlation.code_challenge,
                    EnrollmentCorrelationReservation.release_after > func.clock_timestamp(),
                )
                .with_for_update()
            ).scalar_one_or_none()
            if reservation is None:
                raise EnrollmentRejectedError
            interaction = session.get(EnrollmentInteraction, reservation.interaction_id, with_for_update=True)
            if interaction is None:
                raise EnrollmentRejectedError
            if now >= interaction.expires_at:
                self._expire_interaction(session, interaction, now, "exchange_timeout")
                failure = EnrollmentRejectedError()
            elif interaction.phase is not EnrollmentPhase.ALLOWED:
                failure = (
                    ExchangeAlreadyClaimedError()
                    if interaction.phase in {EnrollmentPhase.EXCHANGING, EnrollmentPhase.COMPLETED}
                    else EnrollmentRejectedError()
                )
            elif client.client_id != correlation.client_id or not granted_scopes <= frozenset(
                interaction.requested_scopes
            ):
                failure = EnrollmentRejectedError()
            else:
                try:
                    self._operator_identities.require_active_in_transaction(session, resolved.operator_id)
                except InactiveOperatorError as error:
                    raise EnrollmentRejectedError from error
                browser_operator = session.scalar(
                    select(IdentityAnchor.operator_id)
                    .join(OidcIdentity, OidcIdentity.anchor_id == IdentityAnchor.anchor_id)
                    .where(OidcIdentity.identity_id == interaction.browser_identity_id)
                )
                if browser_operator is None or browser_operator != resolved.operator_id:
                    raise EnrollmentRejectedError
                client_row = session.get(ClientSoftware, interaction.client_software_id)
                if client_row is None or client_row.oauth_client_id != client.client_id:
                    raise EnrollmentRejectedError
                interaction.phase = EnrollmentPhase.EXCHANGING
                interaction.updated_at = now
                if interaction.reconnect_agent_id is None:
                    name = session.scalar(
                        select(AgentNameReservation)
                        .where(AgentNameReservation.pending_interaction_id == interaction.interaction_id)
                        .with_for_update()
                    )
                    if name is None:
                        raise EnrollmentRejectedError
                    agent_id = uuid4()
                    session.add(
                        Agent(
                            agent_id=agent_id,
                            owner_operator_id=resolved.operator_id,
                            current_name_reservation_id=name.reservation_id,
                            status=AgentStatus.DRAFT,
                            created_at=now,
                            updated_at=now,
                            activated_at=None,
                            last_seen_at=None,
                        )
                    )
                    session.flush()
                    name.pending_interaction_id = None
                    name.agent_id = agent_id
                    name.activated_at = now
                    generation = 1
                    predecessor_id = None
                else:
                    reconnect = session.execute(
                        select(Agent, CredentialBinding)
                        .join(
                            CredentialBinding,
                            CredentialBinding.binding_id == interaction.reconnect_predecessor_binding_id,
                        )
                        .where(Agent.agent_id == interaction.reconnect_agent_id)
                        .with_for_update()
                    ).one_or_none()
                    if reconnect is None:
                        raise EnrollmentRejectedError
                    agent, predecessor = reconnect
                    if (
                        agent.owner_operator_id != resolved.operator_id
                        or agent.status is not AgentStatus.ACTIVE
                        or predecessor.agent_id != agent.agent_id
                        or predecessor.kind is not CredentialKind.OAUTH
                        or predecessor.status is not CredentialBindingStatus.ACTIVE
                    ):
                        raise EnrollmentRejectedError
                    agent_id = agent.agent_id
                    predecessor_id = predecessor.binding_id
                    generation = (
                        session.scalar(
                            select(func.max(CredentialBinding.generation)).where(
                                CredentialBinding.agent_id == agent.agent_id
                            )
                        )
                        or 0
                    ) + 1
                binding_id = uuid4()
                grant_id = uuid4()
                session.add(
                    CredentialBinding(
                        binding_id=binding_id,
                        agent_id=agent_id,
                        kind=CredentialKind.OAUTH,
                        status=CredentialBindingStatus.ISSUING,
                        generation=generation,
                        supersedes_binding_id=predecessor_id,
                        created_at=now,
                        updated_at=now,
                        issued_at=None,
                        activated_at=None,
                        ended_at=None,
                        end_reason=None,
                    )
                )
                session.flush()
                session.add(
                    AuthorizationGrant(
                        grant_id=grant_id,
                        binding_id=binding_id,
                        authorizing_identity_id=resolved.identity_id,
                        client_software_id=interaction.client_software_id,
                        enrollment_interaction_id=interaction.interaction_id,
                        allowed_scopes=sorted(granted_scopes),
                        initial_access_jti=None,
                        initial_refresh_jti=None,
                        token_family_persisted_at=None,
                        created_at=now,
                    )
                )
                authorization = GrantAuthorization(
                    actor=AgentActor(agent_id=agent_id, operator_id=resolved.operator_id, binding_id=binding_id),
                    grant_id=grant_id,
                    client_id=interaction.client_id,
                    allowed_scopes=granted_scopes,
                )
        if failure is not None:
            raise failure
        assert authorization is not None
        return authorization

    async def record_token_family(self, *, grant_id: UUID, evidence: TokenFamilyEvidence) -> None:
        await self._database_call(lambda: self._record_token_family(grant_id, evidence))

    def _record_token_family(self, grant_id: UUID, evidence: TokenFamilyEvidence) -> None:
        if not evidence.access_jti.strip() or (evidence.refresh_jti is not None and not evidence.refresh_jti.strip()):
            raise EnrollmentRejectedError
        now = self._now()
        error: Exception | None = None
        with self._sessions.begin() as session:
            interaction = self._lock_grant_interaction(session, grant_id)
            if interaction is None:
                raise EnrollmentRejectedError
            rows = self._locked_grant_rows(session, grant_id)
            if rows is None:
                raise EnrollmentRejectedError
            grant, binding, _agent, operator, client = rows
            if grant.enrollment_interaction_id != interaction.interaction_id:
                raise EnrollmentRejectedError
            if (
                grant.token_family_persisted_at is not None
                and grant.initial_access_jti == evidence.access_jti
                and grant.initial_refresh_jti == evidence.refresh_jti
                and binding.status in {CredentialBindingStatus.ISSUED, CredentialBindingStatus.ACTIVE}
                and interaction.phase is EnrollmentPhase.COMPLETED
            ):
                return
            if now >= interaction.expires_at:
                self._expire_interaction(session, interaction, now, "token_family_persistence_timeout")
                error = EnrollmentRejectedError()
            elif (
                operator.status is not OperatorStatus.ACTIVE
                or client.oauth_client_id != interaction.client_id
                or interaction.phase is not EnrollmentPhase.EXCHANGING
                or binding.status is not CredentialBindingStatus.ISSUING
                or grant.token_family_persisted_at is not None
            ):
                error = EnrollmentRejectedError()
            else:
                grant.initial_access_jti = evidence.access_jti
                grant.initial_refresh_jti = evidence.refresh_jti
                grant.token_family_persisted_at = now
                binding.status = CredentialBindingStatus.ISSUED
                binding.issued_at = now
                binding.updated_at = now
                interaction.phase = EnrollmentPhase.COMPLETED
                interaction.browser_binding_digest = None
                interaction.closed_at = now
                interaction.closure_reason = "token_family_persisted"
                interaction.updated_at = now
        if error is not None:
            raise error

    async def grant_for_access(
        self, *, grant_id: UUID, client_id: str, token_scopes: frozenset[str]
    ) -> GrantAuthorization:
        return await self._database_call(lambda: self._resolve_grant(grant_id, client_id, token_scopes, activate=False))

    async def grant_for_refresh(
        self, *, grant_id: UUID, client_id: str, requested_scopes: frozenset[str]
    ) -> GrantAuthorization:
        return await self._database_call(
            lambda: self._resolve_grant(grant_id, client_id, requested_scopes, activate=False)
        )

    async def activate_for_tool_call(
        self, *, grant_id: UUID, client_id: str, token_scopes: frozenset[str]
    ) -> GrantAuthorization:
        return await self._database_call(lambda: self._resolve_grant(grant_id, client_id, token_scopes, activate=True))

    def _resolve_grant(
        self, grant_id: UUID, client_id: str, scopes: frozenset[str], *, activate: bool
    ) -> GrantAuthorization:
        now = self._now()
        rejection: GrantRejectedError | None = None
        authorization: GrantAuthorization | None = None
        with self._sessions.begin() as session:
            rows = self._locked_grant_rows(session, grant_id)
            if rows is None:
                raise GrantRejectedError
            grant, binding, agent, operator, client = rows
            allowed = frozenset(grant.allowed_scopes)
            if (
                client.oauth_client_id != client_id
                or not scopes <= allowed
                or grant.token_family_persisted_at is None
                or operator.status is not OperatorStatus.ACTIVE
                or binding.kind is not CredentialKind.OAUTH
            ):
                raise GrantRejectedError
            if self._activation_expired(grant, binding, now):
                self._expire_unactivated_binding(binding, agent, now)
                rejection = GrantRejectedError()
            elif binding.status is CredentialBindingStatus.ISSUED:
                expected_agent_status = (
                    AgentStatus.DRAFT if binding.supersedes_binding_id is None else AgentStatus.ACTIVE
                )
                if agent.status is not expected_agent_status:
                    raise GrantRejectedError
                if activate:
                    if binding.supersedes_binding_id is not None:
                        predecessor = session.get(
                            CredentialBinding, binding.supersedes_binding_id, with_for_update=True
                        )
                        if (
                            predecessor is None
                            or predecessor.agent_id != agent.agent_id
                            or predecessor.status is not CredentialBindingStatus.ACTIVE
                        ):
                            raise GrantRejectedError
                        predecessor.status = CredentialBindingStatus.REVOKED
                        predecessor.ended_at = now
                        predecessor.end_reason = "superseded"
                        predecessor.updated_at = now
                    else:
                        agent.status = AgentStatus.ACTIVE
                        agent.activated_at = now
                    binding.status = CredentialBindingStatus.ACTIVE
                    binding.activated_at = now
                    binding.updated_at = now
                    agent.last_seen_at = now
                    agent.updated_at = now
                authorization = GrantAuthorization(
                    actor=AgentActor(
                        agent_id=agent.agent_id, operator_id=operator.operator_id, binding_id=binding.binding_id
                    ),
                    grant_id=grant_id,
                    client_id=client_id,
                    allowed_scopes=allowed,
                )
            elif binding.status is CredentialBindingStatus.ACTIVE and agent.status is AgentStatus.ACTIVE:
                if activate:
                    agent.last_seen_at = now
                    agent.updated_at = now
                authorization = GrantAuthorization(
                    actor=AgentActor(
                        agent_id=agent.agent_id, operator_id=operator.operator_id, binding_id=binding.binding_id
                    ),
                    grant_id=grant_id,
                    client_id=client_id,
                    allowed_scopes=allowed,
                )
            else:
                rejection = GrantRejectedError()
        if rejection is not None:
            raise rejection
        assert authorization is not None
        return authorization

    async def revoke_grant(self, *, grant_id: UUID) -> None:
        await self._database_call(lambda: self._revoke_grant(grant_id))

    def _revoke_grant(self, grant_id: UUID) -> None:
        now = self._now()
        with self._sessions.begin() as session:
            interaction = self._lock_grant_interaction(session, grant_id)
            if interaction is None:
                raise GrantRejectedError
            rows = self._locked_grant_rows(session, grant_id)
            if rows is None:
                raise GrantRejectedError
            grant, binding, agent, _operator, _client = rows
            if grant.enrollment_interaction_id != interaction.interaction_id:
                raise GrantRejectedError
            if binding.status in {
                CredentialBindingStatus.REVOKED,
                CredentialBindingStatus.EXPIRED,
                CredentialBindingStatus.FAILED,
            }:
                return
            previous_status = binding.status
            binding.status = (
                CredentialBindingStatus.FAILED
                if previous_status is CredentialBindingStatus.ISSUING
                else CredentialBindingStatus.REVOKED
            )
            binding.ended_at = now
            binding.end_reason = "oauth_revocation"
            binding.updated_at = now
            if binding.supersedes_binding_id is None and agent.status is AgentStatus.DRAFT:
                agent.status = AgentStatus.ABANDONED
                agent.updated_at = now
            elif previous_status is CredentialBindingStatus.ACTIVE and agent.status is AgentStatus.ACTIVE:
                agent.status = AgentStatus.DISABLED
                agent.updated_at = now
            if binding.status is CredentialBindingStatus.FAILED and interaction.phase is EnrollmentPhase.EXCHANGING:
                interaction.phase = EnrollmentPhase.FAILED
                interaction.browser_binding_digest = None
                interaction.closed_at = now
                interaction.closure_reason = "revoked_before_issuance"
                interaction.updated_at = now

    @staticmethod
    def _correlation_parts(correlation: AuthorizationCorrelation) -> tuple[str, str, str]:
        return correlation.client_id, correlation.redirect_uri, correlation.code_challenge

    @staticmethod
    def _lock_key(session: Session, namespace: str, *parts: str) -> None:
        key = _canonical_json([namespace, *parts])
        session.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"), {"key": key})

    @staticmethod
    def _validate_authorization_request(request: AuthorizationRequest, upstream_url: str) -> None:
        correlation = request.correlation
        if (
            not correlation.client_id.strip()
            or not correlation.redirect_uri.strip()
            or not correlation.code_challenge.strip()
            or request.client.client_id != correlation.client_id
            or correlation.redirect_uri not in request.client.redirect_uris
            or not upstream_url.strip()
        ):
            raise ValueError("FastMCP authorization request is internally inconsistent")

    @staticmethod
    def _metadata_hash(client: ClientSoftwareSnapshot) -> bytes:
        return hashlib.sha256(
            _canonical_json(
                {
                    "client_id": client.client_id,
                    "display_name": client.display_name,
                    "redirect_uris": list(client.redirect_uris),
                }
            ).encode()
        ).digest()

    def _upsert_client(
        self, session: Session, snapshot: ClientSoftwareSnapshot, now: datetime.datetime
    ) -> ClientSoftware:
        redirects = list(dict.fromkeys(snapshot.redirect_uris))
        row = session.scalar(
            select(ClientSoftware).where(ClientSoftware.oauth_client_id == snapshot.client_id).with_for_update()
        )
        if row is None:
            row = ClientSoftware(
                client_software_id=uuid4(),
                registration_kind=ClientRegistrationKind.OAUTH_PROXY_UNCLASSIFIED,
                oauth_client_id=snapshot.client_id,
                validated_redirect_uris=redirects,
                metadata_hash=self._metadata_hash(snapshot),
                observed_name=snapshot.display_name,
                observed_icon_uri=None,
                created_at=now,
                updated_at=now,
            )
            session.add(row)
            session.flush()
        else:
            row.validated_redirect_uris = redirects
            row.metadata_hash = self._metadata_hash(snapshot)
            row.observed_name = snapshot.display_name
            row.updated_at = now
        return row

    def _require_browser_identity(self, session: Session, browser: EnrollmentBrowserSession) -> None:
        try:
            self._operator_identities.require_active_in_transaction(session, browser.operator_id)
        except InactiveOperatorError as error:
            raise EnrollmentBrowserBindingError from error
        operator_id = session.scalar(
            select(IdentityAnchor.operator_id)
            .join(OidcIdentity, OidcIdentity.anchor_id == IdentityAnchor.anchor_id)
            .where(OidcIdentity.identity_id == browser.identity_id)
        )
        if operator_id != browser.operator_id or not browser.browser_session_id:
            raise EnrollmentBrowserBindingError

    @staticmethod
    def _browser_binding_digest(form_token: str, browser: EnrollmentBrowserSession) -> bytes:
        return _digest(
            "browser-binding",
            form_token,
            browser.browser_session_id,
            str(browser.operator_id),
            str(browser.identity_id),
        )

    @staticmethod
    def _decision_digest(form_token: str, browser: EnrollmentBrowserSession, decision_payload: dict[str, str]) -> bytes:
        return _digest(
            "enrollment-decision",
            form_token,
            browser.browser_session_id,
            str(browser.operator_id),
            str(browser.identity_id),
            _canonical_json(decision_payload),
        )

    def _require_interaction_browser(
        self, interaction: EnrollmentInteraction, browser: EnrollmentBrowserSession, form_token: str
    ) -> None:
        expected = self._browser_binding_digest(form_token, browser)
        if (
            interaction.browser_identity_id != browser.identity_id
            or interaction.browser_binding_digest is None
            or not hmac.compare_digest(interaction.browser_binding_digest, expected)
        ):
            raise EnrollmentBrowserBindingError

    @staticmethod
    def _suggested_name(interaction: EnrollmentInteraction) -> str:
        value = interaction.presentation_snapshot.get("display_name")
        if not isinstance(value, str):
            return "New Agent"
        try:
            return normalize_agent_name(value).display_name
        except InvalidAgentNameError:
            return "New Agent"

    def _enrollment_page(
        self, session: Session, interaction: EnrollmentInteraction, browser: EnrollmentBrowserSession, form_token: str
    ) -> EnrollmentPage:
        reconnect_rows = session.execute(
            select(Agent.agent_id, AgentNameReservation.display_name)
            .join(AgentNameReservation, AgentNameReservation.reservation_id == Agent.current_name_reservation_id)
            .join(CredentialBinding, CredentialBinding.agent_id == Agent.agent_id)
            .where(
                Agent.owner_operator_id == browser.operator_id,
                Agent.status == AgentStatus.ACTIVE,
                CredentialBinding.kind == CredentialKind.OAUTH,
                CredentialBinding.status == CredentialBindingStatus.ACTIVE,
            )
            .order_by(AgentNameReservation.display_name_key)
        ).all()
        client_name = interaction.presentation_snapshot.get("display_name")
        return EnrollmentPage(
            client_software=client_name if isinstance(client_name, str) else interaction.client_id,
            redirect_host=urlsplit(interaction.redirect_uri).netloc,
            requested_scopes=tuple(sorted(interaction.requested_scopes)),
            suggested_agent_name=self._suggested_name(interaction),
            reconnectable_agents=tuple(
                ReconnectableAgent(agent_id=row.agent_id, display_name=row.display_name) for row in reconnect_rows
            ),
            form_token=form_token,
        )

    @staticmethod
    def _expire_interaction(
        session: Session, interaction: EnrollmentInteraction, now: datetime.datetime, reason: str
    ) -> None:
        if interaction.phase in {
            EnrollmentPhase.COMPLETED,
            EnrollmentPhase.DENIED,
            EnrollmentPhase.EXPIRED,
            EnrollmentPhase.FAILED,
        }:
            return
        if interaction.phase is EnrollmentPhase.ALLOWED:
            session.execute(
                delete(AgentNameReservation).where(
                    AgentNameReservation.pending_interaction_id == interaction.interaction_id
                )
            )
        if interaction.phase is EnrollmentPhase.EXCHANGING:
            grant = session.scalar(
                select(AuthorizationGrant).where(
                    AuthorizationGrant.enrollment_interaction_id == interaction.interaction_id
                )
            )
            if grant is not None:
                binding = session.get(CredentialBinding, grant.binding_id, with_for_update=True)
                if binding is not None and binding.status is CredentialBindingStatus.ISSUING:
                    binding.status = CredentialBindingStatus.FAILED
                    binding.ended_at = now
                    binding.end_reason = reason
                    binding.updated_at = now
                    if binding.supersedes_binding_id is None:
                        agent = session.get(Agent, binding.agent_id, with_for_update=True)
                        if agent is not None and agent.status is AgentStatus.DRAFT:
                            agent.status = AgentStatus.ABANDONED
                            agent.updated_at = now
        interaction.phase = EnrollmentPhase.EXPIRED
        interaction.browser_nonce_digest = None
        interaction.browser_binding_digest = None
        interaction.closed_at = now
        interaction.closure_reason = reason
        interaction.updated_at = now

    def _locked_grant_rows(
        self, session: Session, grant_id: UUID
    ) -> tuple[AuthorizationGrant, CredentialBinding, Agent, Operator, ClientSoftware] | None:
        row = session.execute(
            select(AuthorizationGrant, CredentialBinding, Agent, Operator, ClientSoftware)
            .join(CredentialBinding, CredentialBinding.binding_id == AuthorizationGrant.binding_id)
            .join(Agent, Agent.agent_id == CredentialBinding.agent_id)
            .join(Operator, Operator.operator_id == Agent.owner_operator_id)
            .join(ClientSoftware, ClientSoftware.client_software_id == AuthorizationGrant.client_software_id)
            .where(AuthorizationGrant.grant_id == grant_id)
            .with_for_update()
        ).one_or_none()
        if row is None:
            return None
        return row[0], row[1], row[2], row[3], row[4]

    @staticmethod
    def _lock_grant_interaction(session: Session, grant_id: UUID) -> EnrollmentInteraction | None:
        interaction_id = session.scalar(
            select(AuthorizationGrant.enrollment_interaction_id).where(AuthorizationGrant.grant_id == grant_id)
        )
        if interaction_id is None:
            return None
        return session.get(EnrollmentInteraction, interaction_id, with_for_update=True)

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
