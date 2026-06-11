"""Command line utilities for Gatelet management."""

from __future__ import annotations

import argparse
import asyncio
import getpass
import os
import tomllib
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from tomlkit import dumps

from gatelet.server.config import get_settings
from gatelet.server.models import Base, WebhookIntegration, WebhookPayload
from gatelet.server.security import hash_password


def _confirm(prompt: str) -> bool:
    """Prompt user to confirm an action."""
    resp = input(f"{prompt} [y/N]: ").strip().lower()
    return resp == "y"


async def _entity_counts(session: AsyncSession) -> list[tuple[str, int]]:
    """Return row counts for all tables."""
    counts = []
    for table in Base.metadata.sorted_tables:
        result = await session.execute(select(func.count()).select_from(table))
        cnt = result.scalar_one()
        counts.append((table.name, cnt))
    return counts


async def reset_db(*, force: bool = False) -> None:
    """Drop and recreate tables and populate with sample data.

    Args:
        force: If True, skip confirmation prompt (for CI/automated use).
    """
    engine = create_async_engine(str(get_settings().database.dsn), future=True)
    session_factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)

    async with session_factory() as session:
        counts = await _entity_counts(session)
        if any(cnt > 0 for _, cnt in counts) and not force:
            print("Current entity counts:")
            for name, cnt in counts:
                print(f"  {name}: {cnt}")
            if not _confirm("Drop and recreate the database?"):
                print("Aborted.")
                await engine.dispose()
                return

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)

    async with session_factory.begin() as session:
        # Sample integration and payloads for UI demos
        integ = WebhookIntegration(
            name="sample",
            description="Sample integration",
            auth_type="none",
            auth_config={"type": "none"},
            is_enabled=True,
        )
        session.add(integ)
        await session.flush()
        for i in range(3):
            session.add(WebhookPayload(integration_id=integ.id, payload={"sample": i}))
    await engine.dispose()
    print("Database initialized with sample webhook payloads.")


def change_password(config_path: Path, password: str | None) -> None:
    """Change admin password stored in the config file."""
    pwd = password or getpass.getpass("New admin password: ")
    hashed = hash_password(pwd)

    with config_path.open("rb") as f:
        data = tomllib.load(f)

    data.setdefault("admin", {})["password_hash"] = hashed

    with config_path.open("w", encoding="utf-8") as f:
        f.write(dumps(data))

    print(f"Admin password updated in {config_path}.")


def main() -> None:
    """Entry point for command line interface."""
    parser = argparse.ArgumentParser(description="Gatelet management utility")
    sub = parser.add_subparsers(dest="cmd", required=True)

    reset_parser = sub.add_parser("reset-db", help="Initialize a fresh database")
    reset_parser.add_argument("--force", "-f", action="store_true", help="Skip confirmation prompt")
    pw = sub.add_parser("change-password", help="Change admin password")
    pw.add_argument("password", nargs="?", help="New password")

    args = parser.parse_args()
    if args.cmd == "reset-db":
        asyncio.run(reset_db(force=args.force))
    elif args.cmd == "change-password":
        config_path = Path(os.getenv("GATELET_CONFIG", "gatelet.toml"))
        change_password(config_path, args.password)


if __name__ == "__main__":
    main()
