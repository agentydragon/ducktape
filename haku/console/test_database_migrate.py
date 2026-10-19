"""The Console image's database-migration process mode."""

from __future__ import annotations

import pytest
import pytest_bazel

from haku.console import database_migrate


def test_migration_command_reads_only_the_database_url(monkeypatch: pytest.MonkeyPatch) -> None:
    database_url = "postgresql+asyncpg://haku_console:secret@db.example/haku_console"
    monkeypatch.setenv("HAKU_CONSOLE__DATABASE_URL", database_url)
    called: list[str] = []
    monkeypatch.setattr(database_migrate, "apply_migrations", called.append)

    database_migrate.main()

    assert called == [database_url]


if __name__ == "__main__":
    pytest_bazel.main()
