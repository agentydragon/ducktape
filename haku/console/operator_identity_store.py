"""PostgreSQL repository for canonical Haku Operator identities."""

from __future__ import annotations

import datetime
import json
from uuid import UUID, uuid4

from sqlalchemy import select, text
from sqlalchemy.orm import Session, sessionmaker

from haku.console.database_schema import IdentityAnchor, OidcIdentity, Operator
from haku.console.operator_identity import (
    IdentityAnchorKey,
    InactiveOperatorError,
    OperatorIdentityInvariantError,
    OperatorIdentityTrust,
    OperatorStatus,
    ResolvedOperatorIdentity,
    VerifiedExternalIdentity,
)


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


class PostgresOperatorIdentityStore:
    """Resolve trusted external identities to canonical Operator UUIDs.

    Every create path takes the same transaction-scoped advisory lock for an anchor key. This makes
    first contact from the browser and MCP issuers converge on one Operator even when they arrive at
    different replicas concurrently. Hash collisions only serialize unrelated first-contact work;
    correctness still comes from the unique database constraints and the locked re-read.
    """

    def __init__(self, sessions: sessionmaker[Session], trust: OperatorIdentityTrust) -> None:
        # One shared engine/sessionmaker is created in create_app and injected into every store, so
        # the console opens a single connection pool rather than one per store.
        self._session_factory = sessions
        self.trust = trust

    def resolve_verified_identity(self, external_identity: VerifiedExternalIdentity) -> ResolvedOperatorIdentity:
        anchor_key = self.trust.anchor_key(external_identity)
        now = _now()
        with self._session_factory.begin() as session:
            self._lock_anchor(session, anchor_key)
            existing = session.execute(
                select(OidcIdentity, IdentityAnchor, Operator)
                .join(IdentityAnchor, IdentityAnchor.anchor_id == OidcIdentity.anchor_id)
                .join(Operator, Operator.operator_id == IdentityAnchor.operator_id)
                .where(OidcIdentity.issuer == external_identity.issuer)
                .where(OidcIdentity.subject == external_identity.subject)
            ).one_or_none()
            if existing is not None:
                oidc_identity, anchor, operator = existing
                self._assert_anchor(anchor, anchor_key)
                self._require_active(operator)
                oidc_identity.last_seen_at = now
                anchor.updated_at = now
                return ResolvedOperatorIdentity(operator_id=operator.operator_id, identity_id=oidc_identity.identity_id)

            anchor, operator = self._get_or_create_anchor(session, anchor_key, now)
            oidc_identity = OidcIdentity(
                identity_id=uuid4(),
                anchor_id=anchor.anchor_id,
                issuer=external_identity.issuer,
                subject=external_identity.subject,
                first_seen_at=now,
                last_seen_at=now,
            )
            session.add(oidc_identity)
            return ResolvedOperatorIdentity(operator_id=operator.operator_id, identity_id=oidc_identity.identity_id)

    def resolve_configured_external_user_key(self, stable_external_user_key: str) -> UUID:
        """Resolve a controller-fed Authentik user id without fabricating an OIDC identity row."""
        anchor_key = self.trust.configured_anchor_key(stable_external_user_key)
        now = _now()
        with self._session_factory.begin() as session:
            self._lock_anchor(session, anchor_key)
            anchor, operator = self._get_or_create_anchor(session, anchor_key, now)
            anchor.updated_at = now
            return operator.operator_id

    def resolve_active_session(self, *, operator_id: UUID, identity_id: UUID) -> ResolvedOperatorIdentity | None:
        """Revalidate a signed browser session against current identity and Operator state."""
        with self._session_factory() as session:
            row = session.execute(
                select(OidcIdentity.identity_id, IdentityAnchor.operator_id, Operator.status)
                .join(IdentityAnchor, IdentityAnchor.anchor_id == OidcIdentity.anchor_id)
                .join(Operator, Operator.operator_id == IdentityAnchor.operator_id)
                .where(OidcIdentity.identity_id == identity_id)
                .where(IdentityAnchor.operator_id == operator_id)
                .where(IdentityAnchor.trust_domain == self.trust.trust_domain)
                .where(OidcIdentity.issuer.in_(self.trust.trusted_issuers))
            ).one_or_none()
            if row is None or row.status is not OperatorStatus.ACTIVE:
                return None
            return ResolvedOperatorIdentity(operator_id=row.operator_id, identity_id=row.identity_id)

    def is_active(self, operator_id: UUID) -> bool:
        with self._session_factory() as session:
            status = session.scalar(
                select(Operator.status)
                .join(IdentityAnchor, IdentityAnchor.operator_id == Operator.operator_id)
                .where(Operator.operator_id == operator_id)
                .where(IdentityAnchor.trust_domain == self.trust.trust_domain)
                .limit(1)
            )
            return status is OperatorStatus.ACTIVE

    def require_active(self, operator_id: UUID) -> None:
        if not self.is_active(operator_id):
            raise InactiveOperatorError("operator is disabled or missing")

    def require_active_in_transaction(self, session: Session, operator_id: UUID) -> None:
        """Lock and validate an Operator inside a caller-owned database transaction.

        Code that persists or returns an operator-owned capability after external I/O must make
        its final status check in the same transaction as the protected read/write. The row lock
        gives that operation a single linearization point with a concurrent disable: a disable
        committed while the I/O was in flight is observed here, while a later disable waits until
        the capability operation has committed.
        """
        operator = session.get(Operator, operator_id, with_for_update=True)
        if operator is None:
            raise InactiveOperatorError("operator is missing")
        self._require_active(operator)
        anchor_id = session.scalar(
            select(IdentityAnchor.anchor_id)
            .where(IdentityAnchor.operator_id == operator_id)
            .where(IdentityAnchor.trust_domain == self.trust.trust_domain)
            .with_for_update()
            .limit(1)
        )
        if anchor_id is None:
            raise InactiveOperatorError("operator is outside the current identity trust domain")

    @staticmethod
    def _lock_anchor(session: Session, key: IdentityAnchorKey) -> None:
        serialized = json.dumps(
            [key.trust_domain, key.stable_external_user_key], ensure_ascii=True, separators=(",", ":")
        )
        session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:identity_anchor_key, 0))"),
            {"identity_anchor_key": serialized},
        )

    @staticmethod
    def _assert_anchor(anchor: IdentityAnchor, expected: IdentityAnchorKey) -> None:
        if (
            anchor.trust_domain != expected.trust_domain
            or anchor.stable_external_user_key != expected.stable_external_user_key
        ):
            raise OperatorIdentityInvariantError(
                "OIDC identity is attached to an anchor that contradicts its trust-domain mapping"
            )

    @staticmethod
    def _require_active(operator: Operator) -> None:
        if operator.status is not OperatorStatus.ACTIVE:
            raise InactiveOperatorError("operator is disabled")

    def _get_or_create_anchor(
        self, session: Session, key: IdentityAnchorKey, now: datetime.datetime
    ) -> tuple[IdentityAnchor, Operator]:
        row = session.execute(
            select(IdentityAnchor, Operator)
            .join(Operator, Operator.operator_id == IdentityAnchor.operator_id)
            .where(IdentityAnchor.trust_domain == key.trust_domain)
            .where(IdentityAnchor.stable_external_user_key == key.stable_external_user_key)
        ).one_or_none()
        if row is not None:
            anchor, operator = row
            self._require_active(operator)
            return anchor, operator

        operator = Operator(operator_id=uuid4(), status=OperatorStatus.ACTIVE, created_at=now, updated_at=now)
        anchor = IdentityAnchor(
            anchor_id=uuid4(),
            operator_id=operator.operator_id,
            trust_domain=key.trust_domain,
            stable_external_user_key=key.stable_external_user_key,
            created_at=now,
            updated_at=now,
        )
        # These lightweight rows intentionally have no ORM relationships: repository methods own
        # the graph. Flush each FK parent explicitly so SQLAlchemy cannot choose child-first insert
        # order when several new mapped objects have only scalar UUID fields connecting them.
        session.add(operator)
        session.flush()
        session.add(anchor)
        session.flush()
        return anchor, operator
