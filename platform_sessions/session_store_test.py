"""Transactional session recovery, admission, and storage failure boundaries."""

import multiprocessing
import os
from concurrent.futures import ThreadPoolExecutor

import pytest
from pydantic import BaseModel, TypeAdapter, ValidationError
from sqlalchemy import update

from platform_sessions.session_store import ProviderSessionStore, _sessions


class Session(BaseModel):
    token: str


def store(directory):
    return ProviderSessionStore(directory, "example", TypeAdapter(Session))


def test_credentials_and_pending_commit_together(tmp_path):
    original = store(tmp_path)
    original.save(Session(token="old"))
    before = original.snapshot()[1]
    original.begin_operation()
    assert original.load() is None
    assert original.is_pending()
    original.save(Session(token="rotated"))
    recovered = store(tmp_path)
    assert recovered.load() == Session(token="rotated")
    assert not recovered.is_pending()
    assert recovered.snapshot()[1] > before
    assert recovered.storage.path.stat().st_mode & 0o077 == 0
    assert recovered.directory.stat().st_mode & 0o077 == 0


def _interrupt(directory, stage):
    child = store(directory)
    if stage == "before_pending_commit":
        with child.storage.transaction() as connection:
            connection.execute(update(_sessions).values(pending=True, credentials=None))
            os._exit(0)
    child.begin_operation()
    if stage == "after_credentials_commit":
        child.save(Session(token="rotated"))
    os._exit(0)


@pytest.mark.parametrize("stage", ["before_pending_commit", "after_pending_commit", "after_credentials_commit"])
def test_interruption_at_transaction_boundaries_never_replays_consumed_credentials(tmp_path, stage):
    original = store(tmp_path)
    original.save(Session(token="old"))
    child = multiprocessing.get_context("spawn").Process(target=_interrupt, args=(tmp_path, stage))
    child.start()
    child.join(timeout=10)
    assert child.exitcode == 0
    restarted = store(tmp_path)
    if stage == "before_pending_commit":
        # Admission never completed, so no consuming request could be sent.
        assert restarted.load() == Session(token="old")
        assert not restarted.is_pending()
    elif stage == "after_credentials_commit":
        assert restarted.load() == Session(token="rotated")
        assert not restarted.is_pending()
    else:
        assert restarted.load() is None
        assert restarted.is_pending()
        with pytest.raises(ValueError, match="pending"):
            restarted.begin_operation()
        restarted.begin_operation(replace_pending=True)
        restarted.save(Session(token="fresh-login"))
        assert restarted.load() == Session(token="fresh-login")
        assert not restarted.is_pending()


def test_admission_is_exclusive_across_threads_and_connections(tmp_path):
    stores = [store(tmp_path) for _ in range(12)]

    def admit(candidate):
        try:
            candidate.begin_operation()
            return True
        except ValueError:
            return False

    with ThreadPoolExecutor(max_workers=12) as pool:
        admitted = list(pool.map(admit, stores))
    assert sum(admitted) == 1
    assert all(candidate.is_pending() for candidate in stores)


def test_failed_commit_keeps_fence_and_old_credentials_unavailable(tmp_path):
    original = store(tmp_path)
    original.save(Session(token="old"))
    original.begin_operation()
    with original.storage.transaction() as connection:
        connection.exec_driver_sql(
            "CREATE TRIGGER reject_credentials BEFORE UPDATE ON sessions "
            "WHEN NEW.credentials IS NOT NULL BEGIN SELECT RAISE(ABORT, 'injected'); END"
        )
    with pytest.raises(OSError, match="transaction failed"):
        original.save(Session(token="secret-new"))
    restarted = store(tmp_path)
    assert restarted.is_pending()
    assert restarted.load() is None


def test_definitive_rejection_discards_credentials_and_allows_fresh_login(tmp_path):
    original = store(tmp_path)
    original.save(Session(token="old"))
    original.begin_operation()
    original.clear()
    restarted = store(tmp_path)
    assert restarted.load() is None
    assert not restarted.is_pending()
    restarted.begin_operation()
    restarted.save(Session(token="fresh-login"))
    assert restarted.load() == Session(token="fresh-login")


def test_rejects_insecure_or_invalid_database(tmp_path):
    original = store(tmp_path)
    original.storage.path.chmod(0o644)
    with pytest.raises(ValueError, match="private"):
        original.load()
    original.storage.path.chmod(0o600)
    with original.storage.transaction() as connection:
        connection.execute(update(_sessions).values(credentials=b"{}"))
    with pytest.raises(ValidationError):
        original.load()
