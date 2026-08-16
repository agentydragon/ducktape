"""Operator CLI for recording a session as a fixture: export its frames, redacted.

Point it at the console's database — a replica's, a restore, or a throwaway — name a session, and
it writes that session's foldable frames as a JSONL fixture in the form
<claude_code/test_diverse_session.py> reads (<frame_export.py> for the format, and
<claude_code/redaction.py> for what survives redaction and what does not).

```bash
bb run //haku/console/x:frame_export_bin -- \\
    --session <uuid> --output haku/console/x/claude_code/testdata/<name>.jsonl
```

The written file is a **proposal**: read it before checking it in. Redaction is fail-closed by key,
so what it keeps is a short list rather than a judgement about a particular session, and the review
is the second half of that.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Annotated
from uuid import UUID

import typer
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from haku.console.x import frame_export

app = typer.Typer(help=__doc__)

DatabaseUrl = Annotated[str, typer.Option(envvar="HAKU_CONSOLE_DATABASE_URL")]


async def _read(database_url: str, session_id: UUID) -> frame_export.ExportedSession:
    engine = create_async_engine(database_url)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as db:
            return await frame_export.export_session(db, session_id)
    finally:
        await engine.dispose()


@app.command()
def export(
    database_url: DatabaseUrl,
    session: Annotated[UUID, typer.Option(help="The session to export.")],
    output: Annotated[Path, typer.Option(help="Where to write the JSONL fixture.")],
) -> None:
    exported = asyncio.run(_read(database_url, session))
    output.write_text("".join(f"{line}\n" for line in exported.lines()))
    print(f"{exported.summary()} → {output}")


if __name__ == "__main__":
    app()
