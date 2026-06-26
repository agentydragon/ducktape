"""Shared fixtures for haku/console tests: a seeded local haku-state git remote."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pygit2
import pytest
import yaml
from fastapi.testclient import TestClient

from haku.console.app import create_app
from haku.console.config import Settings
from haku.console.git_state import GitState
from haku.console.models import CommandAction, Item, ItemStatus, Suggestion


def _dump_item(item: Item) -> str:
    # safe_dump quotes the all-but-leading-1 ULID id so PyYAML doesn't reload the
    # leading-zero scalar as an (octal) int — which would fail Item's str id field.
    return yaml.safe_dump(item.model_dump(mode="json", exclude_none=True), sort_keys=False, allow_unicode=True)


def _seed_remote(bare: Path, work: Path) -> list[str]:
    """Create a bare repo with a `main` branch holding a few items; return their ids."""
    pygit2.init_repository(str(bare), bare=True, initial_head="main")
    repo = pygit2.clone_repository(str(bare), str(work))
    (work / "items").mkdir(parents=True)
    ids = []
    for i in range(3):
        ulid = f"01{i:024d}"  # 26 chars, valid Crockford base32
        ids.append(ulid)
        item = Item(
            id=ulid,
            title=f"Test item {i}",
            value=90 - i,
            status=ItemStatus.OPEN,
            body="**why** this matters, with `code` and a [link](https://example.com/x).",
            action=Suggestion(),
            actions=[CommandAction(id="snooze", label="Snooze 30d", intent="Snooze this item for 30 days")],
        )
        (work / "items" / f"{ulid}.yaml").write_text(_dump_item(item))
    repo.index.add_all()
    repo.index.write()
    tree = repo.index.write_tree()
    sig = pygit2.Signature("seed", "seed@test")
    repo.create_commit("refs/heads/main", sig, sig, "seed", tree, [])
    repo.remotes["origin"].push(["refs/heads/main"])
    return ids


@pytest.fixture
def seeded(tmp_path: Path) -> SimpleNamespace:
    bare = tmp_path / "remote.git"
    ids = _seed_remote(bare, tmp_path / "seed")
    settings = Settings(git_repo_url=str(bare), git_username="u", git_password="p", clone_dir=tmp_path / "clone")
    git_state = GitState(
        repo_url=settings.git_repo_url,
        username=settings.git_username,
        password=settings.git_password.get_secret_value(),
        clone_dir=settings.clone_dir,
        branch=settings.branch,
    )
    titles = [f"Test item {i}" for i in range(len(ids))]
    return SimpleNamespace(settings=settings, git_state=git_state, ids=ids, titles=titles, bare=bare)


@pytest.fixture
def make_client(seeded: SimpleNamespace) -> Callable[..., Any]:
    """Factory: a TestClient over the seeded remote, with optional `Settings` overrides.
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
