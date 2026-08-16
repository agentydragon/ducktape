"""Operator CLI for the message provenance backfill: recover a message row's frame range.

Point it at the console's database — a replica's, a restore, or a throwaway — and it re-projects
each session's frames and matches the rows carrying no range against what that fold would have
written (<message_provenance.py> for the rule and for what it refuses to guess).

```bash
bb run //haku/console/x:message_provenance_bin -- --limit 500            # dry run, reports only
bb run //haku/console/x:message_provenance_bin -- --limit 500 --apply    # and writes what it found
bb run //haku/console/x:message_provenance_bin -- --session <uuid> --apply
```

The default is the dry run: without `--apply` nothing is written, and the two runs report the same
lines. What is left afterwards is one query — a row with neither a range nor a reason is one this
has not reached:

```sql
SELECT count(*) FROM session_messages
 WHERE source_first_frame_seq IS NULL AND unpointable_reason IS NULL;
```
"""

from __future__ import annotations

import asyncio
from typing import Annotated
from uuid import UUID

import typer
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from haku.console.x import message_provenance

app = typer.Typer(help=__doc__)

DatabaseUrl = Annotated[str, typer.Option(envvar="HAKU_CONSOLE_DATABASE_URL")]


async def _backfill(database_url: str, session_ids: list[UUID], limit: int, write: bool) -> None:
    engine = create_async_engine(database_url)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as db:
            chosen = session_ids or list(await message_provenance.unpointed_sessions(db, limit=limit))
            plans = [await message_provenance.plan(db, session_id) for session_id in chosen]
            for session in plans:
                if write:
                    await message_provenance.apply(db, session)
                    # Per session, so an interrupted run leaves whole sessions done rather than a
                    # session half explained.
                    await db.commit()
            print("\n".join(message_provenance.rendered(plans)))
    finally:
        await engine.dispose()


@app.command()
def backfill(
    database_url: DatabaseUrl,
    session: Annotated[list[UUID] | None, typer.Option(help="Scan exactly these sessions.")] = None,
    limit: Annotated[int, typer.Option(help="How many sessions holding an unexplained row to scan.")] = 100,
    apply: Annotated[bool, typer.Option(help="Write the ranges and reasons instead of reporting them.")] = False,
) -> None:
    asyncio.run(_backfill(database_url, session or [], limit, apply))


if __name__ == "__main__":
    app()
