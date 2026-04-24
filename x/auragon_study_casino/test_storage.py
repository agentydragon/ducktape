"""SQLite state store tests."""

from __future__ import annotations

from pathlib import Path

import pytest_bazel

from x.auragon_study_casino.storage import StateStore


def test_load_before_save_returns_none(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.db")
    assert store.load() is None


def test_save_and_load(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.db")
    record = store.save(b'{"credits":1}')
    loaded = store.load()
    assert loaded is not None
    assert loaded.blob == b'{"credits":1}'
    assert loaded.etag == record.etag


def test_save_overwrites(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.db")
    first = store.save(b'{"credits":1}')
    second = store.save(b'{"credits":2}')
    assert first.etag != second.etag
    loaded = store.load()
    assert loaded is not None
    assert loaded.blob == b'{"credits":2}'


def test_etag_is_deterministic(tmp_path: Path) -> None:
    a = StateStore(tmp_path / "a.db").save(b'{"credits":1}')
    b = StateStore(tmp_path / "b.db").save(b'{"credits":1}')
    assert a.etag == b.etag


def test_data_dir_autocreated(tmp_path: Path) -> None:
    db_path = tmp_path / "nested" / "dir" / "state.db"
    store = StateStore(db_path)
    store.save(b'{"credits":1}')
    assert db_path.exists()


if __name__ == "__main__":
    pytest_bazel.main()
