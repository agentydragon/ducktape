"""Per-user SQLite-backed store for the Study Casino.

Canonical state lives in five small tables:

- `balance` — single row, credits / tokens
- `sessions` — completed study sessions
- `prizes` — user-editable prize catalog
- `prize_log` — append-only redemption log
- `state_snapshots` — JSON dumps before destructive `import`/`reset`

Plus the audit logs `game_events` and `ledger_events`, and the in-flight
`blackjack_hands` table.

Pre-2026-05-08 deployments stored canonical state in a single Y-CRDT
binary blob; the `0004_drop_ydoc_layer` migration backfilled the
relational tables and dropped the blob. From this commit forward the
runtime no longer reads a Y.Doc — every server action mutates ORM rows
inside one SQLite transaction and writes a `ledger_events` row keyed by
`client_action_id` so retried calls are idempotent.
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from alembic import command as alembic_command
from alembic.config import Config as AlembicConfig
from sqlalchemy import create_engine, delete, event, inspect, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from x.auragon_study_casino.events import GameEventRead, LedgerEventRead, game_event_from_row, ledger_event_from_row
from x.auragon_study_casino.games import RULES_VERSION
from x.auragon_study_casino.models import (
    BalanceRow,
    GameEventRow,
    LedgerEventRow,
    PrizeLogRow,
    PrizeRow,
    SessionRow,
    StateSnapshotRow,
)

_MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def _run_alembic_migrations(engine: Engine) -> None:
    """Run pending migrations, baselining pre-Alembic DBs at the doc table.

    Pre-Alembic schemas (DBs predating 0001) had a `doc` table but no
    `alembic_version` row — stamp them at 0001 first so 0001's create-table
    doesn't conflict, then upgrade through to head.
    """
    cfg = AlembicConfig()
    cfg.set_main_option("script_location", str(_MIGRATIONS_DIR))
    with engine.begin() as conn:
        cfg.attributes["connection"] = conn
        tables = set(inspect(conn).get_table_names())
        if "doc" in tables and "alembic_version" not in tables:
            alembic_command.stamp(cfg, "0001")
        alembic_command.upgrade(cfg, "head")


@dataclass(frozen=True)
class ActionRejectedError(Exception):
    """A server action was well-formed but cannot be committed."""

    rule: str
    message: str


@dataclass(frozen=True)
class ActionMutation:
    """Result returned by a server-action mutator before persistence.

    Fields:
        result: the JSON-shaped action result returned to the caller.
        details: free-form metadata persisted in `ledger_events.details_json`.
        game_event: optional dict to fan out into a `game_events` row.
        rng_version: which RNG version (if any) produced this outcome.
        rules_version: which rules version produced this outcome.
    """

    result: dict[str, Any]
    details: dict[str, Any] | None = None
    game_event: dict[str, Any] | None = None
    rng_version: str | None = None
    rules_version: str = RULES_VERSION


@dataclass(frozen=True)
class ServerActionResult:
    """Committed server action."""

    event: LedgerEventRead
    result: dict[str, Any]
    game_event: GameEventRead | None = None


# Mutators take an open Session + the server's `now_ms` and return an
# ActionMutation. They read/write ORM rows directly; the run_server_action
# wrapper handles the surrounding transaction, idempotency check, snapshot,
# and ledger insert.
ServerActionMutator = Callable[[Session, int], ActionMutation]


class SqlStore:
    """Owns one user's SQLite database; runs migrations on open."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._engine: Engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
        with self._engine.connect() as conn:
            conn.exec_driver_sql("PRAGMA journal_mode=WAL")
            conn.commit()
        _run_alembic_migrations(self._engine)
        self._Session = sessionmaker(bind=self._engine, expire_on_commit=False)

    # ── Read-side ───────────────────────────────────────────────────────────

    def state_dump(self) -> dict[str, Any]:
        """Return the full canonical state — same shape as
        `state_snapshots.decoded_json`. The casino frontend's `GET /state`
        returns this verbatim."""
        with self._Session() as s:
            return self._state_dump(s)

    def list_game_events(self, limit: int = 100) -> list[GameEventRead]:
        with self._Session() as s:
            rows = list(s.scalars(select(GameEventRow).order_by(GameEventRow.id.desc()).limit(limit)).all())
            return [game_event_from_row(row) for row in rows]

    def list_ledger_events(self, limit: int = 100) -> list[LedgerEventRead]:
        with self._Session() as s:
            rows = list(s.scalars(select(LedgerEventRow).order_by(LedgerEventRow.id.desc()).limit(limit)).all())
            return [ledger_event_from_row(row) for row in rows]

    # ── Write-side ──────────────────────────────────────────────────────────

    def run_server_action(
        self,
        *,
        client_action_id: str,
        action_type: str,
        mutator: ServerActionMutator,
        snapshot_reason: str | None = None,
        snapshot_note: str | None = None,
    ) -> ServerActionResult:
        """Run one idempotent, server-authoritative mutation.

        Idempotency: if a `ledger_events` row with this `client_action_id`
        already exists, the prior result is returned without replaying the
        mutation. Persistence: the mutation, ledger insert, optional
        `game_events` row, and optional `state_snapshots` row commit in one
        SQLite transaction; either all land or none do.
        """
        with self._Session() as s, s.begin():
            existing = s.scalar(select(LedgerEventRow).where(LedgerEventRow.client_action_id == client_action_id))
            if existing is not None:
                game_event: GameEventRead | None = None
                if existing.action_type.startswith("casino.") or existing.action_type.startswith("blackjack."):
                    game_row = s.scalar(select(GameEventRow).where(GameEventRow.client_event_id == client_action_id))
                    if game_row is not None:
                        game_event = game_event_from_row(game_row)
                return ServerActionResult(
                    event=ledger_event_from_row(existing),
                    result=json.loads(existing.result_json),
                    game_event=game_event,
                )

            now_ms = int(time.time() * 1000)
            balance = self._balance(s)
            before_credits = balance.credits
            before_tokens = balance.tokens

            if snapshot_reason is not None:
                s.add(self._snapshot_row(s, snapshot_reason, now_ms, snapshot_note))

            mutation = mutator(s, now_ms)

            # Re-read balance after mutation; mutator may have changed it.
            s.flush()
            s.refresh(balance)
            after_credits = balance.credits
            after_tokens = balance.tokens

            result_json = self._json(mutation.result)
            details_json = self._json(mutation.details or {})
            event_row = LedgerEventRow(
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

        return ServerActionResult(
            event=ledger_event_from_row(event_row), result=json.loads(result_json), game_event=game_event_out
        )

    # ── Helpers used by import/reset mutators ───────────────────────────────

    def replace_state_for_import(self, s: Session, data: dict[str, Any]) -> None:
        """Wipe sessions/prizes/prize_log + reset balance, then populate from
        the import payload. Must be called from within a `run_server_action`
        mutator (the surrounding transaction guarantees atomicity).
        """
        s.execute(delete(SessionRow))
        s.execute(delete(PrizeRow))
        s.execute(delete(PrizeLogRow))
        balance = self._balance(s)
        balance.credits = int(data.get("credits", 0))
        balance.tokens = int(data.get("tokens", 0))

        for session in data.get("sessions", []) or []:
            session_id = str(session.get("id") or f"imported-{uuid.uuid4()}")
            s.add(
                SessionRow(
                    id=session_id,
                    subject=str(session.get("subject") or "Imported"),
                    seconds=int(session.get("seconds", 0)),
                    ended_at_ms=int(session.get("endedAt") or session.get("ended_at_ms") or 0),
                )
            )

        prizes_data = data.get("prizes") or _DEFAULT_PRIZES_AS_DICTS
        for prize in prizes_data:
            prize_id = str(prize.get("id") or f"p-{uuid.uuid4()}")
            s.add(
                PrizeRow(id=prize_id, name=str(prize.get("name") or "Imported prize"), cost=int(prize.get("cost", 1)))
            )

        for entry in data.get("prizeLog") or data.get("prize_log") or []:
            entry_id = str(entry.get("id") or f"imported-redemption-{uuid.uuid4()}")
            s.add(
                PrizeLogRow(
                    id=entry_id,
                    name=str(entry.get("name") or "Imported prize"),
                    cost=int(entry.get("cost", 0)),
                    at_ms=int(entry.get("at") or entry.get("at_ms") or 0),
                )
            )

    def replace_state_for_reset(self, s: Session) -> None:
        """Wipe sessions/prize_log, zero balance, keep the current prize
        catalog. Equivalent to the legacy `build_reset_casino` behaviour."""
        s.execute(delete(SessionRow))
        s.execute(delete(PrizeLogRow))
        balance = self._balance(s)
        balance.credits = 0
        balance.tokens = 0

    # ── Internal ────────────────────────────────────────────────────────────

    def _balance(self, s: Session) -> BalanceRow:
        balance = s.scalar(select(BalanceRow).where(BalanceRow.id == 1).with_for_update())
        if balance is None:
            # Migration 0004 always inserts the singleton row, but be paranoid
            # so a corrupted DB raises something descriptive instead of None
            # propagating.
            raise RuntimeError("balance row missing — DB was not migrated to 0004")
        return balance

    def _state_dump(self, s: Session) -> dict[str, Any]:
        balance = self._balance(s)
        sessions = list(s.scalars(select(SessionRow).order_by(SessionRow.ended_at_ms.desc())).all())
        prizes = list(s.scalars(select(PrizeRow).order_by(PrizeRow.id)).all())
        prize_log = list(s.scalars(select(PrizeLogRow).order_by(PrizeLogRow.at_ms.desc())).all())
        return {
            "balance": {"credits": balance.credits, "tokens": balance.tokens},
            "sessions": [
                {"id": row.id, "subject": row.subject, "seconds": row.seconds, "ended_at_ms": row.ended_at_ms}
                for row in sessions
            ],
            "prizes": [{"id": row.id, "name": row.name, "cost": row.cost} for row in prizes],
            "prize_log": [{"id": row.id, "name": row.name, "cost": row.cost, "at_ms": row.at_ms} for row in prize_log],
        }

    def _snapshot_row(self, s: Session, reason: str, now_ms: int, note: str | None) -> StateSnapshotRow:
        return StateSnapshotRow(
            server_at_ms=now_ms, reason=reason, decoded_json=self._json(self._state_dump(s)), note=note
        )

    def _game_event_row(
        self,
        *,
        client_event_id: str,
        server_at_ms: int,
        event: dict[str, Any],
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
            client_event_id=client_event_id,
            server_at_ms=server_at_ms,
            occurred_at_ms=server_at_ms,
            game=str(event["game"]),
            event_type="settle",
            source="server_resolved",
            wager_credits=int(event["wager_credits"]),
            payout_tokens=int(event["payout_tokens"]),
            credits_before=credits_before,
            credits_after=credits_after,
            tokens_before=tokens_before,
            tokens_after=tokens_after,
            server_credits=server_credits,
            server_tokens=server_tokens,
            outcome_json=self._json(event["outcome"]),
            rules_version=rules_version,
            rng_version=rng_version,
        )

    @staticmethod
    def _json(value: Any) -> str:
        return json.dumps(value, sort_keys=True, separators=(",", ":"))


# Default prize catalog used by `replace_state_for_import` when the import
# payload doesn't include a `prizes` list. Matches what migration 0004 seeds
# into a fresh DB.
_DEFAULT_PRIZES_AS_DICTS: list[dict[str, Any]] = [
    {"id": "p1", "name": "Anime episode break", "cost": 30},
    {"id": "p2", "name": "Nice coffee shop trip", "cost": 60},
    {"id": "p3", "name": "Takeout night", "cost": 120},
    {"id": "p4", "name": "Nice dinner out with Rai", "cost": 240},
    {"id": "p5", "name": "Buy a new game", "cost": 600},
    {"id": "p6", "name": "Weekend getaway", "cost": 1800},
]


# Enable WAL on every pooled connection.
@event.listens_for(Engine, "connect")
def _sqlite_pragma_on_connect(dbapi_conn: Any, _record: Any) -> None:
    cursor = dbapi_conn.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.close()
