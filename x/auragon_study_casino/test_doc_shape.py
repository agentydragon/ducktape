"""Sanity-check that the pycrdt API we use in `doc_shape.py` actually works.

These are deliberately low-level tests. The interesting business
behaviour lives in `test_validators.py` (post-merge constraint checks)
and `test_doc_store.py` (server-authoritative apply/reject loop). What
we cover here:

- `Casino.empty()` lights up every container with sensible defaults.
- A bootstrap-from-update Casino converges with a server-side Casino.
- Two replicas exchange Y-CRDT updates and converge on disjoint writes.
"""

from __future__ import annotations

import pytest_bazel

from x.auragon_study_casino.doc_shape import DEFAULT_PRIZES, Casino


def test_empty_casino_has_all_containers() -> None:
    casino = Casino.empty()
    assert int(casino.balance["credits"]) == 0
    assert int(casino.balance["tokens"]) == 0
    assert len(casino.active) == 0
    assert len(casino.sessions) == 0
    assert len(casino.prizes) == len(DEFAULT_PRIZES)
    assert len(casino.prize_log) == 0


def test_bootstrap_from_update_carries_schema() -> None:
    """A client who only has a binary update from the server can wake up a
    Casino that reads back the same data the server wrote."""
    server = Casino.empty()
    server.balance["credits"] = 42

    client = Casino.from_update(server.get_update())
    assert int(client.balance["credits"]) == 42
    assert len(client.prizes) == len(DEFAULT_PRIZES)


def test_concurrent_disjoint_writes_converge() -> None:
    """Two clients each touch a different field; after exchanging updates
    both replicas agree on both writes. Same-key concurrent writes resolve
    last-write-wins, which the validators police separately."""
    server = Casino.empty()
    server.balance["credits"] = 100
    base = server.get_update()

    a = Casino.from_update(base)
    b = Casino.from_update(base)

    a.balance["credits"] = 130  # phone earned 30 from a session
    b.balance["tokens"] = 7  # laptop converted 7 credits to tokens

    a_to_b = a.get_update(b.get_state())
    b_to_a = b.get_update(a.get_state())
    a.apply_update(b_to_a)
    b.apply_update(a_to_b)

    assert int(a.balance["credits"]) == 130
    assert int(b.balance["credits"]) == 130
    assert int(a.balance["tokens"]) == 7
    assert int(b.balance["tokens"]) == 7


if __name__ == "__main__":
    pytest_bazel.main()
