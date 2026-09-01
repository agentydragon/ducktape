"""Tests for the common grant lifecycle envelope."""

from __future__ import annotations

import datetime

import pytest_bazel

from haku.console.grants.envelope import GrantStatus, derive_status

_CREATED = datetime.datetime(2026, 8, 21, tzinfo=datetime.UTC)
_EXPIRES = datetime.datetime(2026, 8, 21, 1, tzinfo=datetime.UTC)


def test_status_is_derived_from_one_end_fact_and_the_clock() -> None:
    early = datetime.datetime(2026, 8, 21, 0, 30, tzinfo=datetime.UTC)
    assert derive_status(ended_at=None, expires_at=_EXPIRES, now=_CREATED) is GrantStatus.ACTIVE
    assert derive_status(ended_at=None, expires_at=_EXPIRES, now=_EXPIRES) is GrantStatus.EXPIRED
    assert derive_status(ended_at=early, expires_at=_EXPIRES, now=_EXPIRES) is GrantStatus.ENDED
    # Expiration wins over an end action recorded at or past the time bound.
    assert derive_status(ended_at=_EXPIRES, expires_at=_EXPIRES, now=_EXPIRES) is GrantStatus.EXPIRED


if __name__ == "__main__":
    pytest_bazel.main()
