"""Shared fixtures and helpers for the AI session archive tests."""

import os
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine

from ai_session_archive.database_migrate import RUNNER
from ai_session_archive.restic import ResticRepository, find_pinned_restic_binary
from testing.postgres import DatabaseNames, create_temporary_database


@pytest.fixture
def postgres_engine(postgres_admin_url: str, database_names: DatabaseNames) -> Iterator[Engine]:
    with create_temporary_database(postgres_admin_url, database_names()) as database_url:
        engine = create_engine(database_url)
        try:
            RUNNER.apply(database_url)
            yield engine
        finally:
            engine.dispose()


def _new_repository(tmp_path: Path) -> tuple[ResticRepository, dict[str, str]]:
    repository_path = tmp_path / "repository"
    environment = {
        **os.environ,
        "HOME": str(tmp_path / "home"),
        "RESTIC_REPOSITORY": str(repository_path),
        "RESTIC_PASSWORD": "test-only-passphrase",
        "RESTIC_CACHE_DIR": str(tmp_path / "restic-cache"),
    }
    Path(environment["HOME"]).mkdir()
    Path(environment["RESTIC_CACHE_DIR"]).mkdir()
    binary = find_pinned_restic_binary()
    subprocess.run([binary, "init"], env=environment, check=True, capture_output=True)
    return ResticRepository(binary=binary, environment=environment), environment


def _backup(environment: dict[str, str], *source_paths: Path, force: bool = False, cwd: Path | None = None) -> None:
    arguments = [find_pinned_restic_binary(), "backup"]
    if force:
        arguments.append("--force")
    arguments.extend(("--host", "archive-test", "--", *(str(path) for path in source_paths)))
    subprocess.run(arguments, env=environment, check=True, capture_output=True, cwd=cwd)
