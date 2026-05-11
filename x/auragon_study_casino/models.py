"""SQLAlchemy models for the casino's per-user SQLite database.

Canonical state lives in `balance` (1 row), `sessions`, `prizes`, and
`prize_log`. The `ledger_events` and `game_events` audit logs are
append-only and survive everything that mutates state. `state_snapshots`
keeps a JSON dump before destructive imports/resets so a bad import is
recoverable.

Pre-2026-05-08 deployments stored canonical state as a single Y-CRDT
binary blob in a `doc` table; the `0004_drop_ydoc_layer` migration
backfills the relational tables and drops `doc`.
"""

from __future__ import annotations

from sqlalchemy import CheckConstraint, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class BalanceRow(Base):
    """The single canonical economy row. Always `id = 1`."""

    __tablename__ = "balance"
    __table_args__ = (
        CheckConstraint("id = 1", name="balance_single_row"),
        CheckConstraint("credits >= 0", name="balance_credits_nonneg"),
        CheckConstraint("tokens >= 0", name="balance_tokens_nonneg"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    credits: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class SessionRow(Base):
    """One completed study session.

    In-progress sessions are not persisted server-side — they live in the
    client's localStorage and only become a SessionRow when the user calls
    `/actions/session/complete` (or `/actions/session/add-past` for a
    manually backfilled session). `seconds` is non-negative; `ended_at_ms`
    is the wall-clock end time. `subject` is the user-chosen study topic.
    """

    __tablename__ = "sessions"
    __table_args__ = (
        CheckConstraint("seconds >= 0", name="sessions_seconds_nonneg"),
        CheckConstraint("length(subject) > 0", name="sessions_subject_nonempty"),
    )

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    subject: Mapped[str] = mapped_column(String(120), nullable=False)
    seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    ended_at_ms: Mapped[int] = mapped_column(Integer, nullable=False)


class PrizeRow(Base):
    """One entry in the user-editable prize catalog."""

    __tablename__ = "prizes"
    __table_args__ = (
        CheckConstraint("cost > 0", name="prizes_cost_positive"),
        CheckConstraint("length(name) > 0", name="prizes_name_nonempty"),
    )

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    cost: Mapped[int] = mapped_column(Integer, nullable=False)


class PrizeLogRow(Base):
    """Append-only redemption log. `prize_id` may reference a deleted
    prize; we keep the snapshotted name + cost so the history survives
    catalog edits."""

    __tablename__ = "prize_log"
    __table_args__ = (CheckConstraint("cost >= 0", name="prize_log_cost_nonneg"),)

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    cost: Mapped[int] = mapped_column(Integer, nullable=False)
    at_ms: Mapped[int] = mapped_column(Integer, nullable=False)


class GameEventRow(Base):
    """Append-only server-resolved casino event audit record.

    Every row is server-stamped with the canonical balance observed when
    the event was committed. Pre-cutover rows with `source="client_reported"`
    remain readable but are no longer written.
    """

    __tablename__ = "game_events"
    __table_args__ = (UniqueConstraint("client_event_id", name="game_events_client_event_id_unique"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    client_event_id: Mapped[str] = mapped_column(String(128), nullable=False)
    server_at_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    occurred_at_ms: Mapped[int] = mapped_column(Integer, nullable=False)
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

    Every server action that can affect the economy records one row here. The
    row also stores the action response result so a retried idempotency key can
    return the original committed outcome without replaying the mutation.
    """

    __tablename__ = "ledger_events"
    __table_args__ = (UniqueConstraint("client_action_id", name="ledger_events_client_action_id_unique"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    client_action_id: Mapped[str] = mapped_column(String(128), nullable=False)
    server_at_ms: Mapped[int] = mapped_column(Integer, nullable=False)
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


class StateSnapshotRow(Base):
    """JSON snapshots taken before destructive (`import`/`reset`) actions.

    Pre-0004 rows additionally carried a `doc_update_blob` column with the
    raw Y.Doc binary; that column is dropped by the 0004 migration after
    the relational backfill. `decoded_json` is the human-readable canonical
    state (matches the shape returned by `GET /state`).
    """

    __tablename__ = "state_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    server_at_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    reason: Mapped[str] = mapped_column(String(64), nullable=False)
    decoded_json: Mapped[str] = mapped_column(Text, nullable=False)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)


class BlackjackHandRow(Base):
    """Server-owned blackjack hand state between deal and settlement."""

    __tablename__ = "blackjack_hands"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    created_at_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    wager_credits: Mapped[int] = mapped_column(Integer, nullable=False)
    current_wager_credits: Mapped[int] = mapped_column(Integer, nullable=False)
    credits_before: Mapped[int] = mapped_column(Integer, nullable=False)
    tokens_before: Mapped[int] = mapped_column(Integer, nullable=False)
    shoe_json: Mapped[str] = mapped_column(Text, nullable=False)
    player_json: Mapped[str] = mapped_column(Text, nullable=False)
    dealer_json: Mapped[str] = mapped_column(Text, nullable=False)
    result_json: Mapped[str | None] = mapped_column(Text, nullable=True)
