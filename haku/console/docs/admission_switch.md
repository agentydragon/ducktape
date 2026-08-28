# The prompt-admission switch

A single `runtime_control` row (migration `0110`), read on every prompt admission and flipped by
`POST /api/runtime/admission` (operator-authenticated):

- **`generation`** — the active runtime transport generation. The Console refuses to serve a
  journal runner while the row is absent or names another generation — the fail-safe against an
  image that rolled ahead of its migration. The peering itself is image-carried
  (<../../runtime/x/bridge/neutral_operations.py>); this read is its belt and braces.
- **`admission_closed`** — the operator's drain switch. Closed refuses new prompt admission on
  every surface (channels, SPA, inbox) with `admission_closed`, so a maintenance window drains
  without stopping each surface by hand. It lands open.

`GET /api/runtime/admission` reads both. The escape hatch, for when the API is unreachable, is a
direct `UPDATE runtime_control SET admission_closed = <bool>, updated_at = now() WHERE id = 1;` —
the same row the API writes.

Deliberately a database row, not config: the flip must be transactional with the admissions it
refuses and read identically by every replica, which a per-pod ConfigMap is not.
