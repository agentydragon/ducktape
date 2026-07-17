"""Shared-schema multi-tenant store for the Study Casino.

One SQLAlchemy engine (Postgres; CNPG `study-casino-db` in prod, an
ephemeral testcontainer in tests) backs every user; per-user scoping is
by a `user_id` column on every table. Every server action mutates ORM
rows inside one transaction and writes a `ledger_events` row keyed by
`(user_id, client_action_id)` so retried calls are idempotent.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, get_args

from alembic import command as alembic_command
from alembic.config import Config as AlembicConfig
from pydantic import TypeAdapter
from sqlalchemy import create_engine, delete, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from x.study_casino.actions import ActionResult, ImportData
from x.study_casino.changelog import entries_after, get_or_create_ack
from x.study_casino.credit_award import (
    day_study_seconds,
    get_or_create_credit_state,
    millis_from_credits,
    pacific_date,
    pending_streak_days,
    rest_days_available,
    streak_bonus_percent,
)
from x.study_casino.credit_constants import DAILY_FIRST_BONUS, DAILY_STREAK_STUDY_THRESHOLD_SECONDS
from x.study_casino.events import (
    BlackjackOutcome,
    GameEventMutation,
    GameEventRead,
    GameOutcome,
    LedgerEventRead,
    RouletteOutcome,
    SlotsOutcome,
    game_event_from_row,
    ledger_event_from_row,
)
from x.study_casino.games import RULES_VERSION, theoretical_bucket_rtp
from x.study_casino.models import (
    BalanceRow,
    BlackjackOutcomeKind,
    CreditStateRow,
    Game,
    GameEventRow,
    LedgerEventRow,
    PrizeLogRow,
    PrizeRow,
    RngActionAuditRow,
    RngCallAuditRow,
    RouletteBetType,
    SessionRow,
    SlotsPayoutKind,
    StateSnapshotRow,
)
from x.study_casino.rng import RngActionAudit, canonical_json
from x.study_casino.state import BalanceRead, CreditStateRead, PrizeLogRead, PrizeRead, SessionRead, StateDump
from x.study_casino.stats import (
    SERVER_RESOLVED_SINCE_DATE,
    BlackjackOutcomeFreq,
    BlackjackSlice,
    BlackjackStats,
    BlackjackSummary,
    CasinoStats,
    GameStats,
    TimeBucketStats,
    WagerBucketStats,
)

_MIGRATIONS_DIR = Path(__file__).parent / "migrations"

# Pydantic union of every per-endpoint result shape. Used to parse
# `ledger_events.result_json` back into the typed variant on the
# idempotent-replay path (where we don't have the original Python object).
_ACTION_RESULT_ADAPTER: TypeAdapter[ActionResult] = TypeAdapter(ActionResult)


def _run_alembic_migrations(engine: Engine) -> None:
    cfg = AlembicConfig()
    cfg.set_main_option("script_location", str(_MIGRATIONS_DIR))
    with engine.begin() as conn:
        cfg.attributes["connection"] = conn
        alembic_command.upgrade(cfg, "head")


@dataclass(frozen=True)
class ActionRejectedError(Exception):
    """A server action was well-formed but cannot be committed."""

    rule: str
    message: str


@dataclass(frozen=True)
class ActionMutation:
    """Result returned by a server-action mutator before persistence.

    `result` is the typed payload that surfaces as `ActionResponse.result`
    on the wire. `game_event` is the typed payload that gets persisted to
    `game_events.outcome_json` and re-read as `GameEventRead` by the
    casino-stats and audit-log endpoints. `rng_audit` is the deterministic
    RNG trace, committed atomically with the ledger/game rows. `details`
    stays loosely typed — it's audit metadata persisted to
    `ledger_events.details_json` and not part of the frontend's read surface.
    """

    result: ActionResult
    details: dict[str, Any] | None = None
    game_event: GameEventMutation | None = None
    rng_audit: RngActionAudit | None = None
    rng_version: str | None = None
    rules_version: str = RULES_VERSION


@dataclass(frozen=True)
class ServerActionResult:
    """Committed server action."""

    event: LedgerEventRead
    result: ActionResult
    game_event: GameEventRead | None = None


# Mutators take an open Session + the server's `now_ms` and return an
# ActionMutation. They read/write ORM rows directly; the run_server_action
# wrapper handles the surrounding transaction, idempotency check, snapshot,
# and ledger insert.
ServerActionMutator = Callable[[Session, int], ActionMutation]


def locked_balance(s: Session, username: str) -> BalanceRow:
    """The user's balance row, locked FOR UPDATE for the enclosing transaction.

    Single source of the row-locking strategy for every money mutation (both
    SqlStore internals and app.py mutators). The row must already exist —
    `SqlStore._ensure_user` seeds it at the top of every entry point.
    """
    balance = s.scalar(select(BalanceRow).where(BalanceRow.user_id == username).with_for_update())
    if balance is None:
        raise RuntimeError(f"balance row missing for {username=}; _ensure_user not called?")
    return balance


# Default prize catalog seeded for a user on first contact. Kept in store.py
# (not in a migration) since it is per-user, not per-database. Costs are
# calibrated to the v2 credit economy (see migration 0003's rebalance).
_DEFAULT_PRIZES: list[tuple[str, str, int]] = [
    ("p1", "Anime episode break", 60),
    ("p2", "Nice coffee shop trip", 120),
    ("p3", "Takeout night", 240),
    ("p4", "Nice dinner out with Rai", 480),
    ("p5", "Buy a new game", 1200),
    ("p6", "Weekend getaway", 3600),
]


class SqlStore:
    """Shared-schema store; one engine per process, every method takes `username`."""

    def __init__(self, database_url: str) -> None:
        self._engine: Engine = create_engine(database_url)
        _run_alembic_migrations(self._engine)
        self._Session = sessionmaker(bind=self._engine, expire_on_commit=False)

    # ── Read-side ───────────────────────────────────────────────────────────

    def state_dump(self, username: str) -> StateDump:
        """Return the full canonical state for `username` — same shape as
        `state_snapshots.decoded_json`. The frontend's `GET /state`
        returns this verbatim. Lazily seeds the user on first call."""
        with self._Session() as s, s.begin():
            self._ensure_user(s, username)
            return self._state_dump(s, username)

    def list_known_users(self) -> list[str]:
        """All usernames the store has ever seeded (one balance row apiece)."""
        with self._Session() as s:
            return sorted(s.scalars(select(BalanceRow.user_id)).all())

    def user_exists(self, username: str) -> bool:
        """Whether `username` already has a balance row (i.e. has been seeded)."""
        with self._Session() as s:
            return s.get(BalanceRow, username) is not None

    def list_game_events(self, username: str, limit: int = 100) -> list[GameEventRead]:
        with self._Session() as s:
            rows = list(
                s.scalars(
                    select(GameEventRow)
                    .where(GameEventRow.user_id == username)
                    .order_by(GameEventRow.id.desc())
                    .limit(limit)
                ).all()
            )
            return [game_event_from_row(row) for row in rows]

    def list_ledger_events(self, username: str, limit: int = 100) -> list[LedgerEventRead]:
        with self._Session() as s:
            rows = list(
                s.scalars(
                    select(LedgerEventRow)
                    .where(LedgerEventRow.user_id == username)
                    .order_by(LedgerEventRow.id.desc())
                    .limit(limit)
                ).all()
            )
            return [ledger_event_from_row(row) for row in rows]

    def casino_stats(self, username: str) -> CasinoStats:
        """Aggregate server-resolved `game_events` into per-game / per-wager-type
        and per-UTC-day buckets. `client_reported` rows are excluded — their
        `outcome` payload shape is not guaranteed (legacy SQLite era)."""
        with self._Session() as s:
            rows = list(
                s.scalars(
                    select(GameEventRow)
                    .where(GameEventRow.user_id == username, GameEventRow.source == "server_resolved")
                    .order_by(GameEventRow.id)
                ).all()
            )
        events = [game_event_from_row(row) for row in rows]
        theoretical = theoretical_bucket_rtp()
        blackjack_events = [e for e in events if e.game == "blackjack"]
        games = [
            _aggregate_game(events, "roulette", _ROULETTE_BUCKETS, _roulette_bucket_key, theoretical),
            _aggregate_game(events, "blackjack", _BLACKJACK_BUCKETS, _blackjack_bucket_key, theoretical).model_copy(
                update={"blackjack": _blackjack_stats(blackjack_events)}
            ),
            _aggregate_game(events, "slots", _SLOTS_BUCKETS, _slots_bucket_key, theoretical),
        ]
        return CasinoStats(
            username=username, since_date=SERVER_RESOLVED_SINCE_DATE, event_count=len(events), games=games
        )

    # ── Write-side ──────────────────────────────────────────────────────────

    def run_server_action(
        self,
        *,
        username: str,
        client_action_id: str,
        action_type: str,
        mutator: ServerActionMutator,
        snapshot_reason: str | None = None,
        snapshot_note: str | None = None,
    ) -> ServerActionResult:
        """Run one idempotent, server-authoritative mutation for `username`.

        Idempotency: if a `ledger_events` row with this `(user_id,
        client_action_id)` already exists, the prior result is returned
        without replaying the mutation. Persistence: the mutation, ledger
        insert, optional `game_events` row, and optional `state_snapshots`
        row commit in one transaction; either all land or none do.
        """
        with self._Session() as s, s.begin():
            self._ensure_user(s, username)
            existing = s.scalar(
                select(LedgerEventRow).where(
                    LedgerEventRow.user_id == username, LedgerEventRow.client_action_id == client_action_id
                )
            )
            if existing is not None:
                game_event: GameEventRead | None = None
                if existing.action_type.startswith("casino.") or existing.action_type.startswith("blackjack."):
                    game_row = s.scalar(
                        select(GameEventRow).where(
                            GameEventRow.user_id == username, GameEventRow.client_event_id == client_action_id
                        )
                    )
                    if game_row is not None:
                        game_event = game_event_from_row(game_row)
                return ServerActionResult(
                    event=ledger_event_from_row(existing),
                    result=_ACTION_RESULT_ADAPTER.validate_json(existing.result_json),
                    game_event=game_event,
                )

            now_ms = int(time.time() * 1000)
            balance = locked_balance(s, username)
            before_credits = balance.credits
            before_tokens = balance.tokens

            if snapshot_reason is not None:
                s.add(self._snapshot_row(s, username, snapshot_reason, now_ms, snapshot_note))

            mutation = mutator(s, now_ms)

            # Re-read balance after mutation; mutator may have changed it.
            s.flush()
            s.refresh(balance)
            after_credits = balance.credits
            after_tokens = balance.tokens

            result_json = mutation.result.model_dump_json()
            details_json = canonical_json(mutation.details or {})
            event_row = LedgerEventRow(
                user_id=username,
                client_action_id=client_action_id,
                server_at_ms=now_ms,
                action_type=action_type,
                source="server_action",
                rules_version=mutation.rules_version,
                rng_version=mutation.rng_version,
                credits_before=before_credits,
                credits_after=after_credits,
                tokens_before=before_tokens,
                tokens_after=after_tokens,
                details_json=details_json,
                result_json=result_json,
            )
            s.add(event_row)

            game_event_row: GameEventRow | None = None
            if mutation.game_event is not None:
                game_event_row = self._game_event_row(
                    username=username,
                    client_event_id=client_action_id,
                    server_at_ms=now_ms,
                    event=mutation.game_event,
                    credits_before=before_credits,
                    credits_after=after_credits,
                    tokens_before=before_tokens,
                    tokens_after=after_tokens,
                    server_credits=after_credits,
                    server_tokens=after_tokens,
                    rules_version=mutation.rules_version,
                    rng_version=mutation.rng_version,
                )
                s.add(game_event_row)

            s.flush()
            s.refresh(event_row)
            if game_event_row is not None:
                s.refresh(game_event_row)
                game_event_out = game_event_from_row(game_event_row)
            else:
                game_event_out = None
            if mutation.rng_audit is not None:
                self._persist_rng_audit(
                    s=s,
                    username=username,
                    client_action_id=client_action_id,
                    server_at_ms=now_ms,
                    ledger_event_id=event_row.id,
                    game_event_id=game_event_row.id if game_event_row is not None else None,
                    audit=mutation.rng_audit,
                )

        return ServerActionResult(
            event=ledger_event_from_row(event_row), result=mutation.result, game_event=game_event_out
        )

    # ── Helpers used by import/reset mutators ───────────────────────────────

    def replace_state_for_import(self, s: Session, username: str, data: ImportData) -> None:
        """Wipe sessions/prizes/prize_log + reset balance for `username`, then
        populate from the import payload. Must be called from within a
        `run_server_action` mutator (the surrounding transaction guarantees
        atomicity).
        """
        s.execute(delete(SessionRow).where(SessionRow.user_id == username))
        s.execute(delete(PrizeRow).where(PrizeRow.user_id == username))
        s.execute(delete(PrizeLogRow).where(PrizeLogRow.user_id == username))
        balance = locked_balance(s, username)
        # str() first — Decimal(float) would drag in binary-float artifacts.
        balance.credits = millis_from_credits(Decimal(str(data.credits)))
        balance.tokens = data.tokens

        for session in data.sessions:
            s.add(
                SessionRow(
                    id=session.id or f"imported-{uuid.uuid4()}",
                    user_id=username,
                    subject=session.subject,
                    seconds=session.seconds,
                    ended_at_ms=session.ended_at_ms,
                )
            )

        if data.prizes is None:
            for prize_id, name, cost in _DEFAULT_PRIZES:
                s.add(PrizeRow(id=prize_id, user_id=username, name=name, cost=cost))
        else:
            for prize in data.prizes:
                s.add(PrizeRow(id=prize.id or f"p-{uuid.uuid4()}", user_id=username, name=prize.name, cost=prize.cost))

        for entry in data.prize_log or []:
            s.add(
                PrizeLogRow(
                    id=entry.id or f"imported-redemption-{uuid.uuid4()}",
                    user_id=username,
                    name=entry.name,
                    cost=entry.cost,
                    at_ms=entry.at_ms,
                )
            )

    def replace_state_for_reset(self, s: Session, username: str) -> None:
        """Wipe sessions/prize_log for `username`, zero their balance and
        streak state, keep their prize catalog. Equivalent to the legacy
        `build_reset_casino` behaviour."""
        s.execute(delete(SessionRow).where(SessionRow.user_id == username))
        s.execute(delete(PrizeLogRow).where(PrizeLogRow.user_id == username))
        balance = locked_balance(s, username)
        balance.credits = 0
        balance.tokens = 0
        credit_state = get_or_create_credit_state(s, username)
        credit_state.streak_days = 0
        credit_state.last_qualifying_date = None
        credit_state.rest_days_used = 0
        credit_state.last_first_bonus_date = None

    # ── Internal ────────────────────────────────────────────────────────────

    def _ensure_user(self, s: Session, username: str) -> None:
        """Idempotently seed a balance row and the default prize catalog for `username`.

        Called from the top of every read/write entry point so a brand-new
        user gets a usable starting state without any explicit signup step.
        Composite `(user_id, id)` PKs on `prizes` mean two users can both
        own a prize with `id="p1"` without colliding.
        """
        if s.get(BalanceRow, username) is not None:
            return
        s.add(BalanceRow(user_id=username, credits=0, tokens=0))
        s.add(CreditStateRow(user_id=username, streak_days=0, rest_days_used=0))
        for prize_id, name, cost in _DEFAULT_PRIZES:
            s.add(PrizeRow(user_id=username, id=prize_id, name=name, cost=cost))
        s.flush()

    def _state_dump(self, s: Session, username: str) -> StateDump:
        balance = locked_balance(s, username)
        credit_state = get_or_create_credit_state(s, username)
        changelog_ack = get_or_create_ack(s, username)
        today = pacific_date(int(time.time() * 1000))
        today_iso = today.isoformat()
        sessions = s.scalars(
            select(SessionRow).where(SessionRow.user_id == username).order_by(SessionRow.ended_at_ms.desc())
        ).all()
        prizes = s.scalars(select(PrizeRow).where(PrizeRow.user_id == username).order_by(PrizeRow.id)).all()
        prize_log = s.scalars(
            select(PrizeLogRow).where(PrizeLogRow.user_id == username).order_by(PrizeLogRow.at_ms.desc())
        ).all()
        return StateDump(
            balance=BalanceRead(credits_millis=balance.credits, tokens=balance.tokens),
            credit_state=CreditStateRead(
                streak_days=credit_state.streak_days,
                streak_bonus_percent=streak_bonus_percent(credit_state.streak_days),
                rest_days_available=rest_days_available(credit_state.streak_days, credit_state.rest_days_used),
                daily_bonus_claimed_today=credit_state.last_first_bonus_date == today_iso,
                today_study_seconds=day_study_seconds(s, username, today),
                daily_bonus_threshold_seconds=DAILY_STREAK_STUDY_THRESHOLD_SECONDS,
                daily_bonus_credits=int(DAILY_FIRST_BONUS),
                pending_bonus_percent=streak_bonus_percent(pending_streak_days(credit_state, today)),
            ),
            changelog_unacked=entries_after(changelog_ack.last_acked_id),
            sessions=[
                SessionRead(id=row.id, subject=row.subject, seconds=row.seconds, ended_at_ms=row.ended_at_ms)
                for row in sessions
            ],
            prizes=[PrizeRead(id=row.id, name=row.name, cost=row.cost) for row in prizes],
            prize_log=[PrizeLogRead(id=row.id, name=row.name, cost=row.cost, at_ms=row.at_ms) for row in prize_log],
        )

    def _snapshot_row(self, s: Session, username: str, reason: str, now_ms: int, note: str | None) -> StateSnapshotRow:
        return StateSnapshotRow(
            user_id=username,
            server_at_ms=now_ms,
            reason=reason,
            decoded_json=self._state_dump(s, username).model_dump_json(),
            note=note,
        )

    def _game_event_row(
        self,
        *,
        username: str,
        client_event_id: str,
        server_at_ms: int,
        event: GameEventMutation,
        credits_before: int,
        credits_after: int,
        tokens_before: int,
        tokens_after: int,
        server_credits: int,
        server_tokens: int,
        rules_version: str,
        rng_version: str | None,
    ) -> GameEventRow:
        return GameEventRow(
            user_id=username,
            client_event_id=client_event_id,
            server_at_ms=server_at_ms,
            occurred_at_ms=server_at_ms,
            game=event.game,
            event_type="settle",
            source="server_resolved",
            wager_credits=event.wager_credits,
            payout_tokens=event.payout_tokens,
            credits_before=credits_before,
            credits_after=credits_after,
            tokens_before=tokens_before,
            tokens_after=tokens_after,
            server_credits=server_credits,
            server_tokens=server_tokens,
            outcome_json=event.outcome.model_dump_json(),
            rules_version=rules_version,
            rng_version=rng_version,
        )

    def _persist_rng_audit(
        self,
        *,
        s: Session,
        username: str,
        client_action_id: str,
        server_at_ms: int,
        ledger_event_id: int,
        game_event_id: int | None,
        audit: RngActionAudit,
    ) -> None:
        action_row = RngActionAuditRow(
            user_id=username,
            client_action_id=client_action_id,
            ledger_event_id=ledger_event_id,
            game_event_id=game_event_id,
            server_at_ms=server_at_ms,
            rng_version=audit.rng_version,
            rng_key_id=audit.rng_key_id,
            seed_material_json=audit.seed_material_json,
            seed_digest_hex=audit.seed_digest_hex,
        )
        s.add(action_row)
        s.flush()
        s.refresh(action_row)
        for call in audit.calls:
            s.add(
                RngCallAuditRow(
                    action_audit_id=action_row.id,
                    user_id=username,
                    client_action_id=client_action_id,
                    call_index=call.call_index,
                    purpose=call.purpose,
                    method=call.method,
                    parameters_json=canonical_json(call.parameters),
                    result_json=canonical_json(call.result),
                )
            )


# Buckets are keyed by the typed outcome vocabularies (models.py) so mypy
# proves the keys agree with what games.py emits into outcome_json.
_ROULETTE_BUCKET_LABELS: dict[RouletteBetType, str] = {
    "red": "Red",
    "black": "Black",
    "odd": "Odd",
    "even": "Even",
    "low": "Low (1-18)",
    "high": "High (19-36)",
    "dozen1": "1st dozen",
    "dozen2": "2nd dozen",
    "dozen3": "3rd dozen",
    "number": "Single number",
}
# Key order derives from the Literal, so a new bet type without a label
# fails at import instead of silently dropping a bucket.
_ROULETTE_BUCKETS: list[tuple[RouletteBetType, str]] = [
    (bet_type, _ROULETTE_BUCKET_LABELS[bet_type]) for bet_type in get_args(RouletteBetType)
]
_SLOTS_BUCKETS: list[tuple[SlotsPayoutKind, str]] = [("triple", "Triple"), ("pair", "Pair"), ("none", "No match")]
# Display order (best outcome first), not Literal order.
_BLACKJACK_BUCKETS: list[tuple[BlackjackOutcomeKind, str]] = [
    ("blackjack", "Blackjack"),
    ("win", "Win"),
    ("dealerBust", "Dealer bust"),
    ("push", "Push"),
    ("lose", "Lose"),
    ("bust", "Bust"),
]


@dataclass(frozen=True)
class _TheoreticalStats:
    payout_rate: float | None
    rtp: float | None
    expected_returned: float | None = None
    fair_win_lower_tail_probability: float | None = None


def _roulette_bucket_key(outcome: GameOutcome) -> str | None:
    return outcome.bet_type if isinstance(outcome, RouletteOutcome) else None


def _slots_bucket_key(outcome: GameOutcome) -> str | None:
    return outcome.payout_kind if isinstance(outcome, SlotsOutcome) else None


def _blackjack_bucket_key(outcome: GameOutcome) -> str | None:
    return outcome.outcome if isinstance(outcome, BlackjackOutcome) else None


def _aggregate_game(
    events: list[GameEventRead],
    game: Game,
    bucket_defs: Sequence[tuple[str, str]],
    bucket_key: Callable[[GameOutcome], str | None],
    theoretical: dict[tuple[str, str], tuple[float, float]],
) -> GameStats:
    game_events = [e for e in events if e.game == game]
    by_key: dict[str, list[GameEventRead]] = {key: [] for key, _ in bucket_defs}
    for e in game_events:
        key = bucket_key(e.outcome)
        if key is None or key not in by_key:
            continue
        by_key[key].append(e)

    buckets = [
        _bucket_stats(
            key,
            label,
            by_key[key],
            _theoretical_bucket(
                theoretical.get((game, key)),
                _fair_win_lower_tail_probability(by_key[key], bucket_key, theoretical) if game == "roulette" else None,
            ),
        )
        for key, label in bucket_defs
    ]
    total_theoretical = (
        _weighted_theoretical_for_actual_wagers(game_events, bucket_key, theoretical) if game == "roulette" else None
    )
    total = _bucket_stats("__total__", "All actual wagers", game_events, total_theoretical)

    # Timeline never crosses below the data-collection cutoff — defensive
    # against a backfilled `server_resolved` row whose `occurred_at_ms`
    # somehow predates 2026-05-07.
    by_day: dict[str, list[GameEventRead]] = {}
    for e in game_events:
        day = datetime.fromtimestamp(e.occurred_at_ms / 1000.0, tz=UTC).date().isoformat()
        if day < SERVER_RESOLVED_SINCE_DATE:
            continue
        by_day.setdefault(day, []).append(e)
    timeline = [_time_bucket_stats(day, by_day[day]) for day in sorted(by_day)]

    return GameStats(game=game, total=total, buckets=buckets, timeline=timeline)


def _bucket_stats(
    key: str, label: str, events: list[GameEventRead], theoretical: _TheoreticalStats | None
) -> WagerBucketStats:
    count = len(events)
    wins = sum(1 for e in events if e.payout_tokens > 0)
    wagered = sum(e.wager_credits for e in events)
    returned = sum(e.payout_tokens for e in events)
    net = returned - wagered
    payout_rate = (wins / count) if count > 0 else None
    rtp = (returned / wagered) if wagered > 0 else None
    ev = (net / wagered) if wagered > 0 else None
    theor_p = theoretical.payout_rate if theoretical is not None else None
    theor_rtp = theoretical.rtp if theoretical is not None else None
    theor_ev = (theor_rtp - 1.0) if theor_rtp is not None else None
    expected_returned = (
        theoretical.expected_returned
        if theoretical is not None and theoretical.expected_returned is not None
        else wagered * theor_rtp
        if theor_rtp is not None
        else None
    )
    expected_net = (expected_returned - wagered) if expected_returned is not None else None
    return WagerBucketStats(
        key=key,
        label=label,
        count=count,
        wins=wins,
        wagered=wagered,
        returned=returned,
        net=net,
        expected_returned=expected_returned,
        expected_net=expected_net,
        payout_rate=payout_rate,
        rtp=rtp,
        ev_per_credit=ev,
        theoretical_payout_rate=theor_p,
        theoretical_rtp=theor_rtp,
        theoretical_ev_per_credit=theor_ev,
        fair_win_lower_tail_probability=theoretical.fair_win_lower_tail_probability
        if theoretical is not None
        else None,
    )


def _theoretical_bucket(
    value: tuple[float, float] | None, fair_win_lower_tail_probability: float | None = None
) -> _TheoreticalStats | None:
    if value is None:
        return None
    payout_rate, rtp = value
    return _TheoreticalStats(
        payout_rate=payout_rate, rtp=rtp, fair_win_lower_tail_probability=fair_win_lower_tail_probability
    )


def _weighted_theoretical_for_actual_wagers(
    events: list[GameEventRead],
    bucket_key: Callable[[GameOutcome], str | None],
    theoretical: dict[tuple[str, str], tuple[float, float]],
) -> _TheoreticalStats | None:
    if not events:
        return None

    expected_wins = 0.0
    expected_returned = 0.0
    wagered = 0
    for event in events:
        key = bucket_key(event.outcome)
        if key is None:
            return None
        theor = theoretical.get((event.game, key))
        if theor is None:
            return None
        payout_rate, rtp = theor
        expected_wins += payout_rate
        expected_returned += event.wager_credits * rtp
        wagered += event.wager_credits

    return _TheoreticalStats(
        payout_rate=expected_wins / len(events),
        rtp=(expected_returned / wagered) if wagered > 0 else None,
        expected_returned=expected_returned,
        fair_win_lower_tail_probability=_fair_win_lower_tail_probability(events, bucket_key, theoretical),
    )


def _fair_win_lower_tail_probability(
    events: list[GameEventRead],
    bucket_key: Callable[[GameOutcome], str | None],
    theoretical: dict[tuple[str, str], tuple[float, float]],
) -> float | None:
    """P(fair game has <= observed wins), conditioned on actual wager types."""
    if not events:
        return None

    distribution = {0: 1.0}
    for event in events:
        if event.game != "roulette" or not isinstance(event.outcome, RouletteOutcome):
            return None
        key = bucket_key(event.outcome)
        if key is None:
            return None
        theor = theoretical.get((event.game, key))
        if theor is None:
            return None
        payout_rate, _rtp = theor
        next_distribution: dict[int, float] = {}
        for wins, probability in distribution.items():
            next_distribution[wins] = next_distribution.get(wins, 0.0) + probability * (1.0 - payout_rate)
            next_distribution[wins + 1] = next_distribution.get(wins + 1, 0.0) + probability * payout_rate
        distribution = next_distribution

    observed_wins = sum(1 for event in events if event.payout_tokens > 0)
    return sum(probability for wins, probability in distribution.items() if wins <= observed_wins)


def _time_bucket_stats(date: str, events: list[GameEventRead]) -> TimeBucketStats:
    count = len(events)
    wins = sum(1 for e in events if e.payout_tokens > 0)
    wagered = sum(e.wager_credits for e in events)
    returned = sum(e.payout_tokens for e in events)
    return TimeBucketStats(
        date=date,
        count=count,
        wins=wins,
        wagered=wagered,
        returned=returned,
        net=returned - wagered,
        rtp=(returned / wagered) if wagered > 0 else None,
    )


# Blackjack-specific aggregators ────────────────────────────────────────────────
#
# The generic _aggregate_game treats outcome buckets like roulette bet types,
# but for blackjack the per-outcome rows are tautological (within "Win", every
# hand pays 2× wager, so Win-rate=100%, RTP=200%, EV/credit=+1.0 — by
# definition). These functions produce blackjack-relevant slices instead:
# strategy diagnostics (by dealer upcard, by doubled-or-not) where all
# outcomes can occur and the percent columns carry information.

_BJ_WIN_OUTCOMES: frozenset[BlackjackOutcomeKind] = frozenset({"blackjack", "win", "dealerBust"})
_BJ_LOSS_OUTCOMES: frozenset[BlackjackOutcomeKind] = frozenset({"lose", "bust"})

# 2..9 keep their literal rank; J/Q/K collapse to "10" (same value to the
# dealer); "A" is kept separate because soft-17 logic and bust risk differ
# sharply from a 10-value upcard. Order matters — displayed left-to-right.
_BJ_UPCARD_BUCKETS: list[tuple[str, str]] = [
    ("2", "2"),
    ("3", "3"),
    ("4", "4"),
    ("5", "5"),
    ("6", "6"),
    ("7", "7"),
    ("8", "8"),
    ("9", "9"),
    ("10", "10"),
    ("A", "A"),
]


def _bj_upcard_key(outcome: BlackjackOutcome) -> str | None:
    if not outcome.dealer_cards:
        return None
    rank = outcome.dealer_cards[0].rank
    if rank in ("J", "Q", "K", "10"):
        return "10"
    if rank in ("A", "2", "3", "4", "5", "6", "7", "8", "9"):
        return rank
    return None


def _blackjack_slice(key: str, label: str, events: list[GameEventRead]) -> BlackjackSlice:
    wins = losses = pushes = 0
    wagered = 0
    returned = 0
    for e in events:
        wagered += e.wager_credits
        returned += e.payout_tokens
        assert isinstance(e.outcome, BlackjackOutcome)
        outcome = e.outcome.outcome
        if outcome in _BJ_WIN_OUTCOMES:
            wins += 1
        elif outcome in _BJ_LOSS_OUTCOMES:
            losses += 1
        elif outcome == "push":
            pushes += 1
    net = returned - wagered
    return BlackjackSlice(
        key=key,
        label=label,
        count=len(events),
        wins=wins,
        losses=losses,
        pushes=pushes,
        wagered=wagered,
        returned=returned,
        net=net,
        rtp=(returned / wagered) if wagered > 0 else None,
        ev_per_credit=(net / wagered) if wagered > 0 else None,
    )


def _blackjack_summary(events: list[GameEventRead]) -> BlackjackSummary:
    wins = losses = pushes = blackjacks = busts = 0
    for e in events:
        assert isinstance(e.outcome, BlackjackOutcome)
        outcome = e.outcome.outcome
        if outcome == "blackjack":
            blackjacks += 1
        elif outcome == "bust":
            busts += 1
        if outcome in _BJ_WIN_OUTCOMES:
            wins += 1
        elif outcome in _BJ_LOSS_OUTCOMES:
            losses += 1
        elif outcome == "push":
            pushes += 1
    count = len(events)
    decided = wins + losses
    return BlackjackSummary(
        count=count,
        wins=wins,
        losses=losses,
        pushes=pushes,
        blackjacks=blackjacks,
        busts=busts,
        win_rate_excl_push=(wins / decided) if decided > 0 else None,
        blackjack_rate=(blackjacks / count) if count > 0 else None,
    )


def _blackjack_outcome_freq(events: list[GameEventRead]) -> list[BlackjackOutcomeFreq]:
    total = len(events)
    by_key: dict[str, list[GameEventRead]] = {key: [] for key, _ in _BLACKJACK_BUCKETS}
    for e in events:
        assert isinstance(e.outcome, BlackjackOutcome)
        key = e.outcome.outcome
        if key in by_key:
            by_key[key].append(e)
    return [
        BlackjackOutcomeFreq(
            key=key,
            label=label,
            count=len(by_key[key]),
            freq=(len(by_key[key]) / total) if total > 0 else 0.0,
            avg_wager=(sum(e.wager_credits for e in by_key[key]) / len(by_key[key])) if by_key[key] else 0.0,
        )
        for key, label in _BLACKJACK_BUCKETS
    ]


def _blackjack_upcard_slices(events: list[GameEventRead]) -> list[BlackjackSlice]:
    by_key: dict[str, list[GameEventRead]] = {key: [] for key, _ in _BJ_UPCARD_BUCKETS}
    for e in events:
        assert isinstance(e.outcome, BlackjackOutcome)
        key = _bj_upcard_key(e.outcome)
        if key is not None:
            by_key[key].append(e)
    return [_blackjack_slice(key, label, by_key[key]) for key, label in _BJ_UPCARD_BUCKETS]


def _blackjack_doubled_slices(events: list[GameEventRead]) -> list[BlackjackSlice]:
    def _doubled(e: GameEventRead) -> bool:
        assert isinstance(e.outcome, BlackjackOutcome)
        return e.outcome.doubled

    doubled = [e for e in events if _doubled(e)]
    not_doubled = [e for e in events if not _doubled(e)]
    return [
        _blackjack_slice("doubled", "Doubled", doubled),
        _blackjack_slice("not_doubled", "Not doubled", not_doubled),
    ]


def _blackjack_stats(events: list[GameEventRead]) -> BlackjackStats:
    return BlackjackStats(
        summary=_blackjack_summary(events),
        outcome_freq=_blackjack_outcome_freq(events),
        by_dealer_upcard=_blackjack_upcard_slices(events),
        by_doubled=_blackjack_doubled_slices(events),
    )
