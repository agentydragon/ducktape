"""Round-trip test for the 0004 Y.Doc-blob → relational tables backfill.

The migration is irreversible on production data; this is the only thing
standing between us and silently losing sessions/prizes during the cutover.
"""

from __future__ import annotations

from pathlib import Path

import pytest_bazel
from alembic import command as alembic_command
from alembic.config import Config as AlembicConfig
from pycrdt import Array, Doc, Map
from sqlalchemy import create_engine, text

_MIGRATIONS_DIR = Path(__file__).parent


def _alembic_config(engine) -> AlembicConfig:
    cfg = AlembicConfig()
    cfg.set_main_option("script_location", str(_MIGRATIONS_DIR))
    cfg.attributes["connection"] = engine
    return cfg


def _populate_doc_blob(
    *, credits: int, tokens: int, sessions: list[dict], prizes: list[dict], prize_log: list[dict]
) -> bytes:
    """Build a Y.Doc with the given contents, return its binary update."""
    doc: Doc = Doc()
    bal: Map = doc.get("balance", type=Map)
    sess: Map = doc.get("sessions", type=Map)
    pz: Map = doc.get("prizes", type=Map)
    plog: Array = doc.get("prize_log", type=Array)
    bal["credits"] = credits
    bal["tokens"] = tokens
    for s in sessions:
        sm: Map = Map()
        sess[s["id"]] = sm
        for k, v in s.items():
            if k != "id":
                sm[k] = v
    for p in prizes:
        pm: Map = Map()
        pz[p["id"]] = pm
        for k, v in p.items():
            if k != "id":
                pm[k] = v
    for entry in prize_log:
        em: Map = Map()
        plog.append(em)
        for k, v in entry.items():
            em[k] = v
    return doc.get_update()


def _open_at_0003(tmp_path: Path):
    """Open a fresh SQLite at alembic 0003 (so we can pretend we're a pre-cutover DB)."""
    engine = create_engine(f"sqlite:///{tmp_path / 'casino.db'}", future=True)
    with engine.begin() as conn:
        cfg = _alembic_config(conn)
        alembic_command.upgrade(cfg, "0003")
    return engine


def _upgrade_to_head(engine) -> None:
    with engine.begin() as conn:
        cfg = _alembic_config(conn)
        alembic_command.upgrade(cfg, "head")


def test_backfill_populates_new_tables_from_existing_blob(tmp_path: Path) -> None:
    engine = _open_at_0003(tmp_path)

    blob = _populate_doc_blob(
        credits=42,
        tokens=137,
        sessions=[
            {"id": "s1", "subject": "Biochem", "seconds": 1500, "ended_at_ms": 1_700_000_000_000},
            {"id": "s2", "subject": "Anatomy", "seconds": 600, "ended_at_ms": 1_700_000_001_000},
        ],
        prizes=[{"id": "p1", "name": "Coffee", "cost": 30}, {"id": "p2", "name": "Game", "cost": 600}],
        prize_log=[{"id": "r-1", "name": "Coffee", "cost": 30, "at_ms": 1_700_000_002_000}],
    )
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO doc (id, update_blob) VALUES (1, :blob)"), {"blob": blob})

    _upgrade_to_head(engine)

    with engine.connect() as conn:
        bal = conn.execute(text("SELECT credits, tokens FROM balance WHERE id = 1")).fetchone()
        assert bal == (42, 137)

        rows = conn.execute(text("SELECT id, subject, seconds, ended_at_ms FROM sessions ORDER BY id")).fetchall()
        assert rows == [("s1", "Biochem", 1500, 1_700_000_000_000), ("s2", "Anatomy", 600, 1_700_000_001_000)]

        rows = conn.execute(text("SELECT id, name, cost FROM prizes ORDER BY id")).fetchall()
        assert rows == [("p1", "Coffee", 30), ("p2", "Game", 600)]

        rows = conn.execute(text("SELECT id, name, cost, at_ms FROM prize_log ORDER BY id")).fetchall()
        assert rows == [("r-1", "Coffee", 30, 1_700_000_002_000)]

        # `doc` table is gone; `state_snapshots.doc_update_blob` is gone.
        assert (
            conn.execute(text("SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'doc'")).fetchone()
            is None
        )
        cols = {row[1] for row in conn.execute(text("PRAGMA table_info(state_snapshots)")).fetchall()}
        assert "doc_update_blob" not in cols
        assert "decoded_json" in cols


def test_backfill_fresh_db_seeds_default_prizes(tmp_path: Path) -> None:
    """A DB created from scratch at head (no doc table ever existed) gets
    DEFAULT_PRIZES + a zero balance row."""
    engine = create_engine(f"sqlite:///{tmp_path / 'casino.db'}", future=True)
    with engine.begin() as conn:
        cfg = _alembic_config(conn)
        alembic_command.upgrade(cfg, "head")

    with engine.connect() as conn:
        bal = conn.execute(text("SELECT credits, tokens FROM balance WHERE id = 1")).fetchone()
        assert bal == (0, 0)
        prize_count = conn.execute(text("SELECT count(*) FROM prizes")).scalar()
        assert prize_count == 6  # DEFAULT_PRIZES has 6 entries
        assert conn.execute(text("SELECT count(*) FROM sessions")).scalar() == 0
        assert conn.execute(text("SELECT count(*) FROM prize_log")).scalar() == 0


def test_backfill_skips_in_progress_sessions(tmp_path: Path) -> None:
    """Pre-cutover Y.Docs may carry an in-progress session (no ended_at_ms);
    those don't survive the cutover — active-session state is now
    client-side only."""
    engine = _open_at_0003(tmp_path)

    blob = _populate_doc_blob(
        credits=0,
        tokens=0,
        sessions=[
            {
                "id": "active-1",
                "subject": "Pharmacology",
                "start_time_ms": 1_700_000_000_000,
                "paused": False,
                "paused_duration_ms": 0,
                "pause_started_at_ms": None,
            },
            {"id": "done-1", "subject": "Biochem", "seconds": 1500, "ended_at_ms": 1_700_000_005_000},
        ],
        prizes=[],
        prize_log=[],
    )
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO doc (id, update_blob) VALUES (1, :blob)"), {"blob": blob})

    _upgrade_to_head(engine)

    with engine.connect() as conn:
        rows = conn.execute(text("SELECT id, subject FROM sessions ORDER BY id")).fetchall()
        assert rows == [("done-1", "Biochem")], "active-1 (no ended_at_ms) should not be backfilled"


def test_backfill_empty_blob(tmp_path: Path) -> None:
    """An existing pre-cutover DB whose Y.Doc happens to be empty (no balance,
    no sessions) backfills cleanly to a zero balance + empty tables."""
    engine = _open_at_0003(tmp_path)
    blob = _populate_doc_blob(credits=0, tokens=0, sessions=[], prizes=[], prize_log=[])
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO doc (id, update_blob) VALUES (1, :blob)"), {"blob": blob})

    _upgrade_to_head(engine)

    with engine.connect() as conn:
        assert conn.execute(text("SELECT credits, tokens FROM balance WHERE id = 1")).fetchone() == (0, 0)
        assert conn.execute(text("SELECT count(*) FROM sessions")).scalar() == 0
        assert conn.execute(text("SELECT count(*) FROM prizes")).scalar() == 0
        assert conn.execute(text("SELECT count(*) FROM prize_log")).scalar() == 0


if __name__ == "__main__":
    pytest_bazel.main()
