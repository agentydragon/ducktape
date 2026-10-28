"""Shared fixtures and helpers for the AI session archive tests."""

import os
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine

from util.testing.postgres import DatabaseNames, create_temporary_database
from x.ai_session_archive.database import Base
from x.ai_session_archive.restic import ResticRepository


@pytest.fixture
def postgres_engine(postgres_admin_url: str, database_names: DatabaseNames) -> Iterator[Engine]:
    with create_temporary_database(postgres_admin_url, database_names()) as database_url:
        engine = create_engine(database_url)
        try:
            Base.metadata.create_all(engine)
            yield engine
        finally:
            engine.dispose()


def _restic_binary() -> str:
    roots = [os.environ.get("RUNFILES_DIR"), os.environ.get("TEST_SRCDIR")]
    for runfiles_root in roots:
        if runfiles_root:
            for workspace in ("_main", "ducktape"):
                candidate = Path(runfiles_root) / workspace / "third_party/restic/restic_bin"
                if candidate.is_file():
                    return str(candidate)
    raise AssertionError("pinned Restic binary is missing from test runfiles")


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
    subprocess.run([_restic_binary(), "init"], env=environment, check=True, capture_output=True)
    return ResticRepository(binary=_restic_binary(), environment=environment), environment


def _backup(environment: dict[str, str], *source_paths: Path, force: bool = False) -> None:
    arguments = [_restic_binary(), "backup"]
    if force:
        arguments.append("--force")
    arguments.extend(("--host", "archive-test", *(str(path) for path in source_paths)))
    subprocess.run(arguments, env=environment, check=True, capture_output=True)
