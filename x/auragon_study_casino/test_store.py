"""DocStore: validate-then-persist behaviour and round-trip via SQLite."""

from __future__ import annotations

from pathlib import Path

import pytest
import pytest_bazel
from pycrdt import Map

from x.auragon_study_casino.doc_shape import Casino
from x.auragon_study_casino.store import Accepted, DocStore, Rejected


@pytest.fixture
def store(tmp_path: Path) -> DocStore:
    return DocStore(tmp_path / "casino.db")


def _client_with_initial_state(store: DocStore) -> Casino:
    """Bootstrap a client casino from the server's current update."""
    return Casino.from_update(store.get_update_for_client(None))


def test_seed_state_is_empty(store: DocStore) -> None:
    assert int(store.canonical.balance["credits"]) == 0
    assert int(store.canonical.balance["tokens"]) == 0


def test_round_trip_accepts_valid_update(store: DocStore) -> None:
    client = _client_with_initial_state(store)
    sv = client.get_state()
    client.balance["credits"] = 50

    update = client.get_update(sv)
    result = store.apply_client_update(update, sv)
    assert isinstance(result, Accepted)
    assert int(store.canonical.balance["credits"]) == 50


def test_negative_credits_update_is_rejected_and_canonical_unchanged(store: DocStore) -> None:
    client = _client_with_initial_state(store)
    sv = client.get_state()
    client.balance["credits"] = -10

    result = store.apply_client_update(client.get_update(sv), sv)
    assert isinstance(result, Rejected)
    assert result.rule == "credits_nonneg"
    # canonical state unchanged
    assert int(store.canonical.balance["credits"]) == 0


def test_canonical_persists_across_restart(tmp_path: Path) -> None:
    db = tmp_path / "casino.db"
    store_a = DocStore(db)
    client = _client_with_initial_state(store_a)
    sv = client.get_state()
    client.balance["credits"] = 77
    store_a.apply_client_update(client.get_update(sv), sv)

    store_b = DocStore(db)  # reopen
    assert int(store_b.canonical.balance["credits"]) == 77


def test_two_devices_concurrent_disjoint_updates_both_land(store: DocStore) -> None:
    """Phone bumps credits, laptop adds a session — both persist after sync."""
    base_sv = store.get_server_state_vector()
    base_update = store.get_update_for_client(None)

    phone = Casino.from_update(base_update)
    laptop = Casino.from_update(base_update)

    phone.balance["credits"] = 60  # phone earned 60 from a study session
    laptop.sessions["s1"] = Map()
    laptop.sessions["s1"]["subject"] = "Anatomy"
    laptop.sessions["s1"]["seconds"] = 1500
    laptop.sessions["s1"]["ended_at_ms"] = 1_700_000_000_000

    r1 = store.apply_client_update(phone.get_update(base_sv), base_sv)
    assert isinstance(r1, Accepted)
    r2 = store.apply_client_update(laptop.get_update(base_sv), base_sv)
    assert isinstance(r2, Accepted)

    canonical = store.canonical
    assert int(canonical.balance["credits"]) == 60
    assert "s1" in canonical.sessions
    assert canonical.sessions["s1"]["subject"] == "Anatomy"


def test_server_never_persists_negative_tokens(store: DocStore) -> None:
    """The validator gate guarantees that no client update — however it
    arrives, however it merges — can land canonical with tokens < 0.

    Yjs's last-write-wins resolves *concurrent* writes to the same key
    by (clientId, clock); whichever value LWW picks gets validated
    against the rule. We exercise the strict guarantee with a direct
    write that would unambiguously land negative."""
    boot = _client_with_initial_state(store)
    sv0 = boot.get_state()
    boot.balance["tokens"] = 100
    store.apply_client_update(boot.get_update(sv0), sv0)
    assert int(store.canonical.balance["tokens"]) == 100

    bad = _client_with_initial_state(store)
    bad_sv = bad.get_state()
    bad.balance["tokens"] = -50

    result = store.apply_client_update(bad.get_update(bad_sv), bad_sv)
    assert isinstance(result, Rejected)
    assert result.rule == "tokens_nonneg"
    # Canonical didn't regress into a violating state.
    assert int(store.canonical.balance["tokens"]) == 100


if __name__ == "__main__":
    pytest_bazel.main()
