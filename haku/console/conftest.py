"""Shared fixtures for haku/console tests: a minimal local haku-state git remote."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pygit2
import pytest
from fastapi.testclient import TestClient

from haku.console.app import create_app
from haku.console.config import Settings
from haku.console.git_state import GitState


def _init_remote(bare: Path, work: Path) -> None:
    """Create a bare repo with an empty ``main`` branch (no items — console is item-agnostic)."""
    pygit2.init_repository(str(bare), bare=True, initial_head="main")
    repo = pygit2.clone_repository(str(bare), str(work))
    # Seed with an empty commit so the branch ref exists.
    repo.index.write()
    tree = repo.index.write_tree()
    sig = pygit2.Signature("seed", "seed@test")
    repo.create_commit("refs/heads/main", sig, sig, "init", tree, [])
    repo.remotes["origin"].push(["refs/heads/main"])


@pytest.fixture
def seeded(tmp_path: Path) -> SimpleNamespace:
    bare = tmp_path / "remote.git"
    _init_remote(bare, tmp_path / "seed")
    settings = Settings(git_repo_url=str(bare), git_username="u", git_password="p", clone_dir=tmp_path / "clone")
    git_state = GitState(
        repo_url=settings.git_repo_url,
        username=settings.git_username,
        password=settings.git_password.get_secret_value(),
        clone_dir=settings.clone_dir,
        branch=settings.branch,
    )
    return SimpleNamespace(settings=settings, git_state=git_state, bare=bare)


@pytest.fixture
def make_client(seeded: SimpleNamespace) -> Callable[..., Any]:
    """Factory: a TestClient over the seeded remote, with optional ``Settings`` overrides.
    The context manager runs the lifespan, which clones the remote."""

    @contextmanager
    def _make(**settings_overrides: Any) -> Iterator[TestClient]:
        settings = seeded.settings.model_copy(update=settings_overrides) if settings_overrides else seeded.settings
        with TestClient(create_app(settings, git_state=seeded.git_state)) as c:
            yield c

    return _make


@pytest.fixture
def client(make_client: Callable[..., Any]) -> Iterator[TestClient]:
    with make_client() as c:
        yield c
