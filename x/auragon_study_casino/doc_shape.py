"""Canonical Y.Doc shape for the Study Casino, in pycrdt terms.

The shape is shared between the server (via `pycrdt`) and the client
(via the `yjs` JS library) — both speak the Y-CRDT binary protocol so
the doc on each side is bit-identical after sync. This module is the
single source of truth for the schema: any field added or renamed
here must be mirrored in `frontend/src/sync.js`.

Top-level keys are all on a single root `Doc`:

  balance   : Y.Map        # {"credits": int, "tokens": int}
  active    : Y.Map        # current live study session, or empty when none:
                           #   {"subject": str, "start_time_ms": int,
                           #    "paused": bool, "paused_duration_ms": int,
                           #    "pause_started_at_ms": int|None}
  sessions  : Y.Map[str, Y.Map]   # completed sessions, keyed by client-generated id:
                                  #   {"subject": str, "seconds": int, "ended_at_ms": int}
  prizes    : Y.Map[str, Y.Map]   # prize catalog, keyed by prize id:
                                  #   {"name": str, "cost": int}
  prize_log : Y.Array[Y.Map]      # redemption history, append-only:
                                  #   {"id": str, "name": str,
                                  #    "cost": int, "at_ms": int}

`balance` is two plain numbers (Yjs has no native counter; concurrent
balance writes use last-write-wins via Y.Map). The casino's economy is
enforced post-merge by the validators in `validators.py`, not by CRDT
semantics — see that module for the rationale.

Map values nested inside `sessions` / `prizes` / `prize_log` are
themselves Y.Map / Y.Array values so concurrent edits to a single
session (e.g., editing the subject on phone while editing the
duration on laptop) merge field-by-field instead of clobbering the
whole record.

## pycrdt API gotcha

pycrdt requires every root container to be declared on the receiving
Doc *before* calling `apply_update`, otherwise the root key reads back
as `None`. The `Casino` wrapper below performs that declaration in one
place — both server and a Python client should access the doc through
a `Casino` handle.
"""

from __future__ import annotations

from pycrdt import Array, Doc, Map

DEFAULT_PRIZES: list[tuple[str, str, int]] = [
    ("p1", "Anime episode break", 30),
    ("p2", "Nice coffee shop trip", 60),
    ("p3", "Takeout night", 120),
    ("p4", "Nice dinner out with Rai", 240),
    ("p5", "Buy a new game", 600),
    ("p6", "Weekend getaway", 1800),
]


class Casino:
    """Typed accessor for a casino Y.Doc.

    Declaring the typed handles up front (`doc.get("balance", type=Map)`
    etc.) is required so a freshly created `Doc()` knows what container
    type each root key holds when an inbound `apply_update` lands. The
    wrapper also keeps one obvious place to discover the schema.
    """

    def __init__(self, doc: Doc) -> None:
        self.doc = doc
        self.balance: Map = doc.get("balance", type=Map)
        self.active: Map = doc.get("active", type=Map)
        self.sessions: Map = doc.get("sessions", type=Map)
        self.prizes: Map = doc.get("prizes", type=Map)
        self.prize_log: Array = doc.get("prize_log", type=Array)

    @classmethod
    def empty(cls) -> Casino:
        """Build a freshly-seeded casino with default credits, tokens and
        prize catalogue. Used by the server on first boot; clients should
        instead call `from_update()` against the server's initial blob."""
        casino = cls(Doc())
        casino.balance["credits"] = 0
        casino.balance["tokens"] = 0
        for prize_id, name, cost in DEFAULT_PRIZES:
            prize = Map()
            casino.prizes[prize_id] = prize
            casino.prizes[prize_id]["name"] = name
            casino.prizes[prize_id]["cost"] = cost
        return casino

    @classmethod
    def from_update(cls, update: bytes) -> Casino:
        """Bootstrap a casino from the server's binary update blob."""
        casino = cls(Doc())
        casino.doc.apply_update(update)
        return casino

    def get_update(self, since_state: bytes | None = None) -> bytes:
        """Binary update from `since_state` to current; full update when None
        or empty.

        pycrdt rejects `get_update(b"")` with "Cannot decode state" — clients
        connecting for the first time naturally have nothing to encode, so
        we accept empty bytes here and treat them like `None`."""
        if not since_state:
            return self.doc.get_update()
        return self.doc.get_update(since_state)

    def get_state(self) -> bytes:
        return self.doc.get_state()

    def apply_update(self, update: bytes) -> None:
        self.doc.apply_update(update)
