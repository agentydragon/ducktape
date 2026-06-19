"""Shared fixtures for haku/arm tests: a seeded local haku-state git remote."""

from __future__ import annotations

import textwrap
from pathlib import Path
from types import SimpleNamespace

import pygit2
import pytest

from haku.arm.config import Settings
from haku.arm.git_state import GitState

_ITEM = textwrap.dedent("""\
    id: "{id}"
    dedup_key: {dk}
    title: "{title}"
    value: {value}
    source: test
    status: open
    body: |
      **why** this matters, with `code` and a [link](https://example.com/x).
    action:
      kind: suggestion
    actions:
      - id: snooze
        label: "Snooze 30d"
        kind: command
        intent: "Snooze this item for 30 days"
""")


def _seed_remote(bare: Path, work: Path) -> list[str]:
    """Create a bare repo with a `main` branch holding a few items; return their ids."""
    pygit2.init_repository(str(bare), bare=True, initial_head="main")
    repo = pygit2.clone_repository(str(bare), str(work))
    (work / "items").mkdir(parents=True)
    ids = []
    for i in range(3):
        # 26 chars, valid Crockford base32. All-digit, so it must be YAML-quoted
        # (see _ITEM) or PyYAML reads the leading-zero scalar as an octal int.
        ulid = f"01{i:024d}"
        ids.append(ulid)
        (work / "items" / f"{ulid}.yaml").write_text(
            _ITEM.format(id=ulid, dk=f"dk-{i}", title=f"Test item {i}", value=90 - i)
        )
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
