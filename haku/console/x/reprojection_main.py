"""Operator CLI for the projection drift check: re-project sessions, print where the rows differ.

Point it at the console's database — a replica's, a restore, or a throwaway — and it re-folds each
session's recorded frames and aligns the result against `session_events`
(<reprojection.py> for what it aligns and what it deliberately skips).

```bash
bb run //haku/console/x:reprojection_bin -- --session <uuid>   # one session
bb run //haku/console/x:reprojection_bin -- --limit 50         # the 50 newest sessions with turns
```

Exit status is 1 when any turn drifted, so a standing run of it is a check rather than a report.
A skipped turn is not a failure: it is a turn the check cannot speak about, and it says why.
"""

from __future__ import annotations

import asyncio
from typing import Annotated
from uuid import UUID

import typer
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from haku.console.x import reprojection

app = typer.Typer(help=__doc__)

DatabaseUrl = Annotated[str, typer.Option(envvar="HAKU_CONSOLE_DATABASE_URL")]


async def _check(database_url: str, session_ids: list[UUID], limit: int, only_findings: bool) -> bool:
    engine = create_async_engine(database_url)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as db:
            chosen = session_ids or list(await reprojection.recent_sessions(db, limit=limit))
            drifted = False
            for session_id in chosen:
                report = await reprojection.check_session(db, session_id)
                drifted = drifted or bool(report.drifted)
                if report.drifted or not only_findings:
                    print("\n".join(reprojection.rendered(report)))
            return drifted
    finally:
        await engine.dispose()


@app.command()
def check(
    database_url: DatabaseUrl,
    session: Annotated[list[UUID] | None, typer.Option(help="Check exactly these sessions.")] = None,
    limit: Annotated[int, typer.Option(help="How many of the newest sessions with turns to check.")] = 20,
    only_findings: Annotated[bool, typer.Option(help="Print only sessions that drifted.")] = False,
) -> None:
    if asyncio.run(_check(database_url, session or [], limit, only_findings)):
        raise typer.Exit(code=1)


if __name__ == "__main__":
    app()
