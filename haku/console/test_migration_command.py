"""The Console image exposes a minimal migration-only process mode."""

from __future__ import annotations

import pytest
import pytest_bazel
from pydantic import ValidationError

from haku.console import app, database_migrate


def test_migration_command_reads_only_the_database_url(monkeypatch: pytest.MonkeyPatch) -> None:
    database_url = "postgresql+asyncpg://approval_store:secret@db.example/approval_store"
    monkeypatch.setenv("HAKU_CONSOLE__DATABASE_URL", database_url)
    called: list[str] = []
    monkeypatch.setattr(database_migrate, "apply_migrations", called.append)

    database_migrate.main()

    assert called == [database_url]


def test_migration_command_rejects_an_absent_database_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HAKU_CONSOLE__DATABASE_URL", raising=False)

    with pytest.raises(ValidationError, match="database_url"):
        database_migrate.main()


def test_image_command_dispatches_migration_without_starting_the_api(monkeypatch: pytest.MonkeyPatch) -> None:
    called: list[str] = []
    monkeypatch.setattr(app, "migration_main", lambda: called.append("migrate"))
    monkeypatch.setattr(app, "main", lambda: called.append("serve"))

    app.run_command(["migrate"])

    assert called == ["migrate"]


def test_server_startup_checks_schema_without_applying_migrations(monkeypatch: pytest.MonkeyPatch) -> None:
    class DatabaseUrl:
        @staticmethod
        def get_secret_value() -> str:
            return "postgresql+asyncpg://approval_store:secret@db.example/approval_store"

    class TestSettings:
        database_url = DatabaseUrl()

    checked: list[str] = []

    async def serve_without_binding(_app: object) -> None:
        pass

    monkeypatch.setattr(app, "Settings", TestSettings)
    monkeypatch.setattr(app, "load_static_agents", lambda settings: [])
    monkeypatch.setattr(app, "verify_schema", checked.append)
    monkeypatch.setattr(app, "create_app", lambda settings, loaded_static_agents: object())
    # The schema check under test runs before main() serves; stub the serve step so the test
    # neither binds a port nor enters the event loop.
    monkeypatch.setattr(app, "_serve", serve_without_binding)

    app.main()

    assert checked == ["postgresql+asyncpg://approval_store:secret@db.example/approval_store"]


def test_image_command_rejects_unknown_modes() -> None:
    with pytest.raises(SystemExit, match="usage"):
        app.run_command(["unknown"])


if __name__ == "__main__":
    pytest_bazel.main()
