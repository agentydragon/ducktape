"""The migration chain must be a single line, and nothing in CI checks that today.

Five times on 2026-08-17, two branches open at once each read the same head and each minted the
next integer. Every one was invisible to the tools that were looking: `git merge-tree` reports the
pair as merging cleanly, because the two files have different names and share no line, and
`bazel test` passes on each branch alone, because each branch's own chain is perfectly linear. The
fault exists only in the merge, which is the one tree nothing ran.

What two of those land is a database with two heads, or two revisions claiming one id — either way
an `alembic upgrade head` that raises and a console that cannot boot. Running here puts the check
on the merge commit, which is where the fault first becomes visible.

No database and no container: `ScriptDirectory` is the loader `database_migrate` already uses, so
this reads the chain exactly as the runner would.
"""

from __future__ import annotations

from pathlib import Path

import pytest_bazel
from alembic.config import Config as AlembicConfig
from alembic.script import ScriptDirectory

_MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def _scripts() -> ScriptDirectory:
    cfg = AlembicConfig()
    cfg.set_main_option("script_location", str(_MIGRATIONS_DIR))
    return ScriptDirectory.from_config(cfg)


def test_the_chain_has_exactly_one_head() -> None:
    """Two heads is what two branches produce when both chain onto the same parent.

    `get_heads` rather than `get_current_head`, which raises on more than one — the heads are the
    thing being asserted, so a failure should name them rather than blow up reaching for them.
    """
    assert len(_scripts().get_heads()) == 1


def test_every_revision_file_is_on_the_chain() -> None:
    """A revision the walk does not reach is one no upgrade would apply.

    Two ways that happens, and this catches both: a duplicate id, where Alembic's revision map
    silently keeps whichever file loaded last and the other simply vanishes; and an orphan whose
    `down_revision` names something real but which nothing names in turn.
    """
    on_chain = {revision.revision for revision in _scripts().walk_revisions()}
    declared = [
        line.removeprefix("revision: str = ").strip().strip('"')
        for path in sorted(_MIGRATIONS_DIR.glob("versions/*.py"))
        for line in path.read_text().splitlines()
        if line.startswith("revision: str = ")
    ]
    assert sorted(declared) == sorted(on_chain)


if __name__ == "__main__":
    pytest_bazel.main()
