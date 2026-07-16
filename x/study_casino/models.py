"""SQLAlchemy models for the casino's shared-schema multi-tenant database.

Every per-user table carries a `user_id` column; all reads are scoped
`WHERE user_id = :user`. One Postgres DB (CNPG `study-casino-db` in prod;
an ephemeral testcontainer in tests) backs every user.

Canonical state lives in `balance` (one row per user), `sessions`, `prizes`,
and `prize_log`. The `ledger_events`, `game_events`, and `rng_*_audits`
audit logs are append-only and survive everything that mutates state.
`state_snapshots` keeps a JSON dump before destructive imports/resets so a
bad import is recoverable.
"""

from __future__ import annotations

from sqlalchemy import BigInteger, CheckConstraint, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


# String length used for every `user_id` column. Matches the OIDC `sub`-shaped
# usernames the casino accepts (≤64 chars).
_USER_ID = String(length=64)


class BalanceRow(Base):
    """One canonical economy row per user.

    `credits` is stored as integer **millicredits** (credit value × 1000) — see
    `credit_award.py`. Tokens are whole integers.
    """

    __tablename__ = "balance"
    __table_args__ = (
        CheckConstraint("credits >= 0", name="balance_credits_nonneg"),
        CheckConstraint("tokens >= 0", name="balance_tokens_nonneg"),
    )

    user_id: Mapped[str] = mapped_column(_USER_ID, primary_key=True)
    credits: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class CreditStateRow(Base):
    """Per-user streak and daily-bonus state (credit system v2).

    Dates are ISO `YYYY-MM-DD` strings in Pacific time. Append-only
    semantics: session edits/deletes never rewind this state.
    """

    __tablename__ = "credit_state"

    user_id: Mapped[str] = mapped_column(_USER_ID, primary_key=True)
    streak_days: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_qualifying_date: Mapped[str | None] = mapped_column(String(10), nullable=True)
    rest_days_used: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_first_bonus_date: Mapped[str | None] = mapped_column(String(10), nullable=True)


class SessionRow(Base):
    """One completed study session for a user.

    PK is composite `(user_id, id)` so two users can independently mint
    the same row id without colliding.
    """

    __tablename__ = "sessions"
    __table_args__ = (
        CheckConstraint("seconds >= 0", name="sessions_seconds_nonneg"),
        CheckConstraint("length(subject) > 0", name="sessions_subject_nonempty"),
        Index("idx_sessions_user_ended_at", "user_id", "ended_at_ms"),
    )

    user_id: Mapped[str] = mapped_column(_USER_ID, primary_key=True)
    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    subject: Mapped[str] = mapped_column(String(120), nullable=False)
    seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    ended_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)


class PrizeRow(Base):
    """One entry in a user's prize catalog. Composite PK `(user_id, id)`."""

    __tablename__ = "prizes"
    __table_args__ = (
        CheckConstraint("cost > 0", name="prizes_cost_positive"),
        CheckConstraint("length(name) > 0", name="prizes_name_nonempty"),
    )

    user_id: Mapped[str] = mapped_column(_USER_ID, primary_key=True)
    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    cost: Mapped[int] = mapped_column(Integer, nullable=False)


class PrizeLogRow(Base):
    """Append-only redemption log. `prize_id` may reference a deleted prize.
    Composite PK `(user_id, id)`."""

    __tablename__ = "prize_log"
    __table_args__ = (
        CheckConstraint("cost >= 0", name="prize_log_cost_nonneg"),
        Index("idx_prize_log_user_at_ms", "user_id", "at_ms"),
    )

    user_id: Mapped[str] = mapped_column(_USER_ID, primary_key=True)
    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    cost: Mapped[int] = mapped_column(Integer, nullable=False)
    at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)


class GameEventRow(Base):
    """Append-only server-resolved casino event audit record.

    `credits_before/after` and `server_credits` are balance snapshots in
    integer millicredits; `wager_credits` is the wager in whole credits.
    """

    __tablename__ = "game_events"
    __table_args__ = (
        UniqueConstraint("user_id", "client_event_id", name="game_events_user_client_event_id_unique"),
        Index("idx_game_events_user_id", "user_id", "id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(_USER_ID, nullable=False)
    client_event_id: Mapped[str] = mapped_column(String(128), nullable=False)
    server_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    occurred_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    game: Mapped[str] = mapped_column(String(32), nullable=False)
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False, default="server_resolved")
    wager_credits: Mapped[int] = mapped_column(Integer, nullable=False)
    payout_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    credits_before: Mapped[int] = mapped_column(Integer, nullable=False)
    credits_after: Mapped[int] = mapped_column(Integer, nullable=False)
    tokens_before: Mapped[int] = mapped_column(Integer, nullable=False)
    tokens_after: Mapped[int] = mapped_column(Integer, nullable=False)
    server_credits: Mapped[int] = mapped_column(Integer, nullable=False)
    server_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    outcome_json: Mapped[str] = mapped_column(Text, nullable=False)
    rules_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    rng_version: Mapped[str | None] = mapped_column(String(32), nullable=True)


class LedgerEventRow(Base):
    """Append-only server-authoritative action log.

    `credits_before/after` are balance snapshots in integer millicredits.
    """

    __tablename__ = "ledger_events"
    __table_args__ = (
        UniqueConstraint("user_id", "client_action_id", name="ledger_events_user_client_action_id_unique"),
        Index("idx_ledger_events_user_id", "user_id", "id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(_USER_ID, nullable=False)
    client_action_id: Mapped[str] = mapped_column(String(128), nullable=False)
    server_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    action_type: Mapped[str] = mapped_column(String(64), nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    rules_version: Mapped[str] = mapped_column(String(32), nullable=False)
    rng_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    credits_before: Mapped[int] = mapped_column(Integer, nullable=False)
    credits_after: Mapped[int] = mapped_column(Integer, nullable=False)
    tokens_before: Mapped[int] = mapped_column(Integer, nullable=False)
    tokens_after: Mapped[int] = mapped_column(Integer, nullable=False)
    details_json: Mapped[str] = mapped_column(Text, nullable=False)
    result_json: Mapped[str] = mapped_column(Text, nullable=False)


class RngActionAuditRow(Base):
    """One deterministic RNG seed context for a committed server action."""

    __tablename__ = "rng_action_audits"
    __table_args__ = (
        UniqueConstraint("user_id", "client_action_id", name="rng_action_audits_user_client_action_id_unique"),
        Index("idx_rng_action_audits_user_id", "user_id", "id"),
        Index("idx_rng_action_audits_ledger_event_id", "ledger_event_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(_USER_ID, nullable=False)
    client_action_id: Mapped[str] = mapped_column(String(128), nullable=False)
    ledger_event_id: Mapped[int] = mapped_column(ForeignKey("ledger_events.id"), nullable=False)
    game_event_id: Mapped[int | None] = mapped_column(ForeignKey("game_events.id"), nullable=True)
    server_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    rng_version: Mapped[str] = mapped_column(String(32), nullable=False)
    rng_key_id: Mapped[str] = mapped_column(String(64), nullable=False)
    seed_material_json: Mapped[str] = mapped_column(Text, nullable=False)
    seed_digest_hex: Mapped[str] = mapped_column(String(64), nullable=False)


class RngCallAuditRow(Base):
    """One recorded deterministic RNG call within a server action."""

    __tablename__ = "rng_call_audits"
    __table_args__ = (
        UniqueConstraint("action_audit_id", "call_index", name="rng_call_audits_action_call_unique"),
        Index("idx_rng_call_audits_action_id", "action_audit_id", "call_index"),
        Index("idx_rng_call_audits_user_action", "user_id", "client_action_id", "call_index"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    action_audit_id: Mapped[int] = mapped_column(ForeignKey("rng_action_audits.id"), nullable=False)
    user_id: Mapped[str] = mapped_column(_USER_ID, nullable=False)
    client_action_id: Mapped[str] = mapped_column(String(128), nullable=False)
    call_index: Mapped[int] = mapped_column(Integer, nullable=False)
    purpose: Mapped[str] = mapped_column(String(128), nullable=False)
    method: Mapped[str] = mapped_column(String(32), nullable=False)
    parameters_json: Mapped[str] = mapped_column(Text, nullable=False)
    result_json: Mapped[str] = mapped_column(Text, nullable=False)


class StateSnapshotRow(Base):
    """JSON snapshots taken before destructive (`import`/`reset`) actions."""

    __tablename__ = "state_snapshots"
    __table_args__ = (Index("idx_state_snapshots_user_id", "user_id", "id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(_USER_ID, nullable=False)
    server_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    reason: Mapped[str] = mapped_column(String(64), nullable=False)
    decoded_json: Mapped[str] = mapped_column(Text, nullable=False)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)


class BlackjackHandRow(Base):
    """Server-owned blackjack hand state between deal and settlement.
    Composite PK `(user_id, id)`. `credits_before` is a balance snapshot in
    integer millicredits; the wager columns are whole credits."""

    __tablename__ = "blackjack_hands"
    __table_args__ = (Index("idx_blackjack_hands_user_status", "user_id", "status"),)

    user_id: Mapped[str] = mapped_column(_USER_ID, primary_key=True)
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    created_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    updated_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    wager_credits: Mapped[int] = mapped_column(Integer, nullable=False)
    current_wager_credits: Mapped[int] = mapped_column(Integer, nullable=False)
    credits_before: Mapped[int] = mapped_column(Integer, nullable=False)
    tokens_before: Mapped[int] = mapped_column(Integer, nullable=False)
    shoe_json: Mapped[str] = mapped_column(Text, nullable=False)
    player_json: Mapped[str] = mapped_column(Text, nullable=False)
    dealer_json: Mapped[str] = mapped_column(Text, nullable=False)
    result_json: Mapped[str | None] = mapped_column(Text, nullable=True)
