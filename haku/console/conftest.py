"""Shared fixtures for haku/console tests: a seeded local haku-state git remote."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

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
            source="test",
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
def client(seeded: SimpleNamespace) -> Iterator[TestClient]:
    """App over the seeded remote; the context manager runs the lifespan, which clones it."""
    app = create_app(seeded.settings, git_state=seeded.git_state)
    with TestClient(app) as c:
        yield c
