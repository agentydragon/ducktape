"""The shared envelope of a Console grant, consolidated across grant domains (#4889).

Every grant domain stores the same envelope around a domain-typed coverage: the grant
principal that receives the permission, the validity window, the lifecycle owner, and the
immutable manually-approved source-ToolCall provenance that doubles as the audit linkage.
This module is that envelope's one definition — the stored column shape
(:class:`GrantEnvelopeColumns` plus :func:`grant_envelope_table_args`), the returned model
shape (:class:`GrantEnvelope`), the lifecycle vocabulary (:class:`GrantStatus`,
:func:`derive_status`), the error family, and the applicability/idempotency logic every
domain repository shares. The granted *what* stays typed per domain: Kubernetes keeps
RequestAttributes/SAR semantics, HTTP keeps exact canonical origins.

Storage is one table per domain sharing these envelope columns, not one principals table
with per-domain bindings: applicability filters need no join, no principal row can be
orphaned, and each row mirrors the :data:`~haku.console.grants.principal.GrantPrincipal`
discriminated union under its table's principal-shape CHECK.

The end-facts half of the lifecycle is deliberately not here yet: Kubernetes stores a
``status`` column with ``ended_at`` while HTTP derives status from ``released_at``/
``revoked_at`` (#4883 dissolves the stored column onto the derived shape, once, against
this envelope). The source-provenance query invariant lives in
:mod:`haku.console.grants.provenance`, split from this module so ``database_schema`` can
import the column mixin without a cycle.
"""

from __future__ import annotations

import datetime
from collections import Counter
from collections.abc import Callable, Sequence
from enum import StrEnum
from typing import Annotated, Final
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import CheckConstraint, ColumnElement, DateTime, ForeignKey, Index, Text, and_, or_
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from haku.console.grants.principal import (
    AgentGrantPrincipal,
    GrantPrincipal,
    GrantPrincipalKind,
    RequestPrincipal,
    grant_principal_from_columns,
)
from util.sqlalchemy_types import TextBackedStrEnumColumn

NON_EMPTY = Annotated[str, Field(min_length=1)]

# One bound for every batched grant operation: creation by one source ToolCall, release and
# revocation by one call. The Agent-facing tool schemas carry the same bound.
GRANT_SET_LIMIT: Final = 32


class GrantStatus(StrEnum):
    ACTIVE = "active"
    RELEASED = "released"
    REVOKED = "revoked"
    EXPIRED = "expired"


def derive_status(
    *,
    released_at: datetime.datetime | None,
    revoked_at: datetime.datetime | None,
    expires_at: datetime.datetime,
    now: datetime.datetime,
) -> GrantStatus:
    """Compute the lifecycle vocabulary from the end facts and the clock.

    Expiration wins over an end action recorded at or after ``expires_at``, so a late release or
    revocation cannot revive or relabel a lease that had already reached its time bound.
    """

    if released_at is not None and released_at < expires_at:
        return GrantStatus.RELEASED
    if revoked_at is not None and revoked_at < expires_at:
        return GrantStatus.REVOKED
    return GrantStatus.EXPIRED if now >= expires_at else GrantStatus.ACTIVE


class GrantError(Exception):
    """Base class for grant-domain failures."""


class GrantNotFoundError(GrantError, LookupError):
    pass


class GrantOwnershipError(GrantError, PermissionError):
    pass


class GrantSourceError(GrantError, ValueError):
    pass


class GrantEnvelope(BaseModel):
    """Domain-independent half of a durable grant returned by a grant service.

    The end facts — ``released_at``, ``revoked_at``, ``end_reason`` — live here: at most one
    end action ever exists, and :func:`derive_status` computes the lifecycle vocabulary from
    the facts and the clock. ``status`` is deliberately not an envelope field: HTTP computes
    it from these facts (`HttpGrant.status`), while Kubernetes still carries its stored
    column until the #4883 contract step flips its readers onto the facts.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    grant_id: UUID
    owner_agent_id: UUID
    principal: GrantPrincipal
    source_tool_call_id: NON_EMPTY
    created_at: AwareDatetime
    expires_at: AwareDatetime
    released_at: AwareDatetime | None = None
    revoked_at: AwareDatetime | None = None
    end_reason: str | None = None

    @model_validator(mode="after")
    def validate_principal_owner(self) -> GrantEnvelope:
        # Session ownership is a relational invariant enforced while persisting/reconstructing the
        # grant: a globally unique session ID intentionally does not duplicate its Agent ID here.
        if isinstance(self.principal, AgentGrantPrincipal) and self.principal.agent_id != self.owner_agent_id:
            raise ValueError("Agent grant principals must belong to the lifecycle owner")
        return self

    @model_validator(mode="after")
    def validate_window(self) -> GrantEnvelope:
        if self.expires_at <= self.created_at:
            raise ValueError("expires_at must be after created_at")
        return self

    @model_validator(mode="after")
    def validate_single_end_action(self) -> GrantEnvelope:
        if self.released_at is not None and self.revoked_at is not None:
            raise ValueError("a grant cannot be both released and revoked")
        return self


class GrantEnvelopeColumns:
    """Envelope columns shared by every per-domain grant table, mapped via declarative mixin.

    Each domain keeps its own table and adds its typed coverage and end-fact columns; the
    envelope columns and :func:`grant_envelope_table_args` keep the shared shape identical
    across them. Lifecycle ownership (``owner_agent_id``) and authorization applicability
    (the ``principal_*`` triple) are deliberately separate columns; ``source_tool_call_id``
    is immutable provenance referring to the Agent-authenticated source call.
    """

    grant_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    owner_agent_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("agents.agent_id", ondelete="RESTRICT"), nullable=False
    )
    principal_kind: Mapped[GrantPrincipalKind] = mapped_column(
        TextBackedStrEnumColumn(GrantPrincipalKind), nullable=False
    )
    principal_agent_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("agents.agent_id", ondelete="RESTRICT"), nullable=True
    )
    principal_session_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("sessions.session_id", ondelete="RESTRICT"), nullable=True
    )
    source_tool_call_id: Mapped[str] = mapped_column(
        Text, ForeignKey("mcp_tool_calls.tool_call_id", ondelete="RESTRICT"), nullable=False
    )
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # The end facts derive_status reads. Expiry deliberately records no fact — it derives from
    # ``expires_at`` and the clock alone.
    released_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    end_reason: Mapped[str | None] = mapped_column(Text, nullable=True)


def grant_envelope_table_args(table: str) -> tuple[CheckConstraint | Index, ...]:
    """The envelope's shared constraints and indexes, named per table."""

    return (
        CheckConstraint("btrim(source_tool_call_id) <> ''", name=f"ck_{table}_source_tool_call_nonempty"),
        CheckConstraint(
            "(principal_kind = 'agent' AND principal_agent_id IS NOT NULL "
            "AND principal_agent_id = owner_agent_id AND principal_session_id IS NULL) OR "
            "(principal_kind = 'session' AND principal_agent_id IS NULL "
            "AND principal_session_id IS NOT NULL)",
            name=f"ck_{table}_principal_shape",
        ),
        CheckConstraint("expires_at > created_at", name=f"ck_{table}_expiration_after_creation"),
        Index(f"idx_{table}_source_tool_call", "source_tool_call_id"),
    )


def request_principal_clause(
    row: type[GrantEnvelopeColumns], request_principal: RequestPrincipal
) -> ColumnElement[bool]:
    """Filter ``row``'s table to the grants this authenticated request principal may exercise.

    A session request principal inherits its Agent's grants; a static credential has no session
    identity, so exact-session grants never match it (`grant_principal_applies_to`'s contract, as
    one SQL clause).
    """

    grant_principals = [
        and_(row.principal_kind == GrantPrincipalKind.AGENT, row.principal_agent_id == request_principal.agent_id)
    ]
    if request_principal.session_id is not None:
        grant_principals.append(
            and_(
                row.owner_agent_id == request_principal.agent_id,
                row.principal_kind == GrantPrincipalKind.SESSION,
                row.principal_session_id == request_principal.session_id,
            )
        )
    return or_(*grant_principals)


def match_replayed_grant_set[RowT: GrantEnvelopeColumns](
    existing: Sequence[RowT], *, grant_principal: GrantPrincipal, specs: Sequence[str], row_spec: Callable[[RowT], str]
) -> tuple[RowT, ...]:
    """Answer an idempotent replay of one source ToolCall's grant set from its existing rows.

    A source ToolCall creates exactly one immutable grant set: a replay carrying the same
    principal and the same spec multiset gets the stored rows back, ordered to match the
    request; anything else raises :class:`GrantSourceError`. ``row_spec`` projects one stored
    row onto the domain's canonical spec JSON, the comparison key requests are counted by.
    """

    if any(
        grant_principal_from_columns(
            row.principal_kind, agent_id=row.principal_agent_id, session_id=row.principal_session_id
        )
        != grant_principal
        for row in existing
    ):
        raise GrantSourceError("source_tool_call_id already created a different grant principal")
    if Counter(row_spec(row) for row in existing) != Counter(specs):
        raise GrantSourceError("source_tool_call_id already created a different grant set")
    rows_by_spec: dict[str, list[RowT]] = {}
    for row in existing:
        rows_by_spec.setdefault(row_spec(row), []).append(row)
    return tuple(rows_by_spec[spec].pop() for spec in specs)


def aware_now(clock: Callable[[], datetime.datetime]) -> datetime.datetime:
    now = clock()
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("grant service clock must return a timezone-aware datetime")
    return now


def validate_grant_window(
    *, now: datetime.datetime, expires_at: datetime.datetime, max_lifetime: datetime.timedelta
) -> None:
    if expires_at.tzinfo is None or expires_at.utcoffset() is None:
        raise ValueError("expires_at must be timezone-aware")
    if expires_at <= now:
        raise ValueError("expires_at must be in the future")
    if expires_at > now + max_lifetime:
        raise ValueError("expires_at exceeds the configured grant lifetime")


def validated_grant_set[SpecT](source_tool_call_id: str, grants: Sequence[SpecT]) -> tuple[SpecT, ...]:
    if not source_tool_call_id:
        raise ValueError("source_tool_call_id must not be empty")
    grants = tuple(grants)
    if not grants:
        raise ValueError("grants must not be empty")
    if len(grants) > GRANT_SET_LIMIT:
        raise ValueError(f"at most {GRANT_SET_LIMIT} grants may be created by one ToolCall")
    return grants


def validated_end_batch(grant_ids: Sequence[UUID], reason: str) -> tuple[tuple[UUID, ...], str]:
    grant_ids = tuple(grant_ids)
    if not grant_ids:
        raise ValueError("grant_ids must not be empty")
    if len(grant_ids) > GRANT_SET_LIMIT:
        raise ValueError(f"at most {GRANT_SET_LIMIT} grants may be ended by one call")
    if len(set(grant_ids)) != len(grant_ids):
        raise ValueError("grant_ids must not contain duplicates")
    reason = reason.strip()
    if not reason:
        raise ValueError("grant end reason must not be empty")
    return grant_ids, reason
