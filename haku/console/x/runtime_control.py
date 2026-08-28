"""The operator's admission switch, and who may read or flip it (#4667).

Two facts on the singleton `runtime_control` row (<../database_schema.py>), created by its own
migration and read on every prompt admission and journal-runner admission:

- **generation** — the active runtime transport generation. A runner presents its build's on the
  journal hello; the peering is image-carried, and this read is its belt and braces: `None` (no
  row yet), or another generation, refuses to serve — the fail-safe against an image that rolled
  ahead of its migration.
- **admission** — the operator's drain switch. Closed refuses new prompt admission so a
  maintenance window drains without stopping each surface by hand.

Reads take a caller's `AsyncSession` so the admission check commits with the prompt it gates; the
flip opens its own transaction. Nothing here decides *whether* the code's generation is right — that
is `neutral_operations.GENERATION`; this only reports what the database says is active.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast

from sqlalchemy import CursorResult, update
from sqlalchemy.ext.asyncio import AsyncSession

from haku.console.database_schema import RuntimeControl

_ROW_ID = 1


async def _row(db: AsyncSession) -> RuntimeControl | None:
    return await db.get(RuntimeControl, _ROW_ID)


async def active_generation(db: AsyncSession) -> str | None:
    """The active transport generation, or None pre-cut (the control row does not exist yet)."""
    row = await _row(db)
    return None if row is None else row.generation


async def admission_closed(db: AsyncSession) -> bool:
    """Whether new prompt admission is currently closed.

    Pre-cut (no row) reads open: the switch is a post-cut drain control, and the v3 world it has
    not yet replaced admits as it always did.
    """
    row = await _row(db)
    return row is not None and row.admission_closed


async def set_admission(db: AsyncSession, *, closed: bool) -> None:
    """Open or close admission, inside the caller's transaction.

    Raises `LookupError` before the cut: there is no switch to flip until the migration has created
    it, and silently succeeding would tell the operator admission is closed when nothing enforces it.
    """
    result = cast(
        "CursorResult[Any]",
        await db.execute(
            update(RuntimeControl)
            .where(RuntimeControl.id == _ROW_ID)
            .values(admission_closed=closed, updated_at=datetime.now(UTC))
        ),
    )
    if result.rowcount == 0:
        raise LookupError("no runtime_control row: its migration has not been applied, so there is nothing to flip")
