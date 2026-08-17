"""0060 gives the cached Matrix token and the sync watermark a table each."""

from __future__ import annotations

import pytest_bazel
from sqlalchemy import Connection, create_engine, text

from haku.console.database_migrate import apply_migrations, sync_database_url


def _state(conn: Connection, user_id: str, *, access_token: str | None, next_batch: str | None) -> None:
    conn.execute(
        text("INSERT INTO matrix_sync_state (user_id, access_token, next_batch) VALUES (:u, :t, :b)"),
        {"u": user_id, "t": access_token, "b": next_batch},
    )


def test_both_live_values_are_carried_across_and_a_null_becomes_no_row(db_url: str) -> None:
    """The data is worth keeping in both directions: a lost watermark replays or skips events, and a
    lost token spends a `/login` Synapse rate-limits. A column that was NULL says nothing was there,
    which in the new shape is no row rather than a row with a null in it."""
    apply_migrations(db_url, "0059")
    engine = create_engine(sync_database_url(db_url))
    try:
        with engine.begin() as conn:
            _state(conn, "@both:example.org", access_token="syt_live", next_batch="s99")
            _state(conn, "@token-only:example.org", access_token="syt_fresh", next_batch=None)
            _state(conn, "@watermark-only:example.org", access_token=None, next_batch="s7")
            _state(conn, "@neither:example.org", access_token=None, next_batch=None)

        apply_migrations(db_url)

        with engine.connect() as conn:
            tokens = dict(conn.execute(text("SELECT user_id, access_token FROM matrix_access_token")).tuples().all())
            watermarks = dict(
                conn.execute(text("SELECT user_id, next_batch FROM matrix_sync_watermark")).tuples().all()
            )
        assert tokens == {"@both:example.org": "syt_live", "@token-only:example.org": "syt_fresh"}
        assert watermarks == {"@both:example.org": "s99", "@watermark-only:example.org": "s7"}
    finally:
        engine.dispose()


def test_the_previous_image_can_still_read_and_write_its_own_row(db_url: str) -> None:
    """The roll-safety half: `matrix_sync_state` is untouched, so a replica on the previous image
    keeps selecting both columns and keeps writing them for the length of the roll."""
    apply_migrations(db_url)
    engine = create_engine(sync_database_url(db_url))
    try:
        with engine.begin() as conn:
            _state(conn, "@old-replica:example.org", access_token="syt_old", next_batch="s1")
            conn.execute(
                text("UPDATE matrix_sync_state SET next_batch = 's2' WHERE user_id = :u"),
                {"u": "@old-replica:example.org"},
            )

        with engine.connect() as conn:
            assert conn.execute(
                text("SELECT access_token, next_batch FROM matrix_sync_state WHERE user_id = :u"),
                {"u": "@old-replica:example.org"},
            ).one() == ("syt_old", "s2")
    finally:
        engine.dispose()


if __name__ == "__main__":
    pytest_bazel.main()
