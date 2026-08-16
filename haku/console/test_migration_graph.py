"""Two branches can each add a migration numbered 0043 and merge without a textual conflict.

Different filenames, same `down_revision` — git merges both happily and neither diff shows anything
wrong. What lands is a revision graph that is no longer a line: two files claiming the same revision
id (Alembic keeps whichever it read last, so the other migration never runs), or two heads
(`alembic upgrade head` refuses to pick one). `database_migrate.apply_migrations` runs at console
startup, so either shape surfaces as a console that will not boot on the deploy after the second
migration merges.

Reads the revision files off disk — no database, no `env.py` execution.
"""

from __future__ import annotations

import ast
from collections import defaultdict
from pathlib import Path

import pytest
import pytest_bazel
from alembic.config import Config
from alembic.script import ScriptDirectory

_MIGRATIONS = Path(__file__).parent / "migrations"

_RENUMBER = (
    "Renumber all but one: take the next free number, rename the file, set its `revision` to match, "
    "and point its `down_revision` at the revision it now follows."
)


def _files_declaring(revision: str, revision_by_file: dict[str, str]) -> str:
    return ", ".join(sorted(name for name, declared in revision_by_file.items() if declared == revision))


def _module_level_constant(path: Path, name: str) -> str | None:
    """The value a version file assigns to `name` at module level."""
    for node in ast.parse(path.read_text()).body:
        match node:
            case (
                ast.AnnAssign(target=ast.Name(id=assigned), value=ast.Constant(value=value))
                | ast.Assign(targets=[ast.Name(id=assigned)], value=ast.Constant(value=value))
            ) if assigned == name:
                assert value is None or isinstance(value, str), (
                    f"{path.name} assigns `{name} = {value!r}`; Alembic revision ids are strings"
                )
                return value
    raise AssertionError(f"{path.name} assigns no module-level `{name}`, so Alembic sees no migration in it")


def _declared_revision(path: Path) -> str:
    revision = _module_level_constant(path, "revision")
    assert isinstance(revision, str), f"{path.name} declares `revision = {revision!r}`, which Alembic cannot use"
    return revision


@pytest.fixture(scope="module")
def version_files() -> list[Path]:
    return sorted((_MIGRATIONS / "versions").glob("*.py"))


@pytest.fixture(scope="module")
def revision_by_file(version_files: list[Path]) -> dict[str, str]:
    return {path.name: _declared_revision(path) for path in version_files}


@pytest.fixture(scope="module")
def down_revision_by_file(version_files: list[Path]) -> dict[str, str | None]:
    return {path.name: _module_level_constant(path, "down_revision") for path in version_files}


@pytest.fixture(scope="module")
def script_directory() -> ScriptDirectory:
    config = Config()
    config.set_main_option("script_location", str(_MIGRATIONS))
    return ScriptDirectory.from_config(config)


def test_each_revision_id_is_declared_by_exactly_one_file(revision_by_file: dict[str, str]) -> None:
    files_by_revision: dict[str, list[str]] = defaultdict(list)
    for name, revision in revision_by_file.items():
        files_by_revision[revision].append(name)
    collisions = {revision: names for revision, names in files_by_revision.items() if len(names) > 1}
    assert not collisions, (
        "Two migrations claim the same revision id: "
        + "; ".join(f"{revision} in {' and '.join(names)}" for revision, names in sorted(collisions.items()))
        + ". Alembic does not fail on this — it warns, keeps whichever file it read last, and the other "
        f"migration never runs against any database. {_RENUMBER}"
    )


def test_every_down_revision_names_a_migration_that_exists(
    revision_by_file: dict[str, str], down_revision_by_file: dict[str, str | None]
) -> None:
    declared = set(revision_by_file.values())
    dangling = {name: down for name, down in down_revision_by_file.items() if down is not None and down not in declared}
    assert not dangling, (
        "A migration follows a revision no file declares: "
        + "; ".join(f"{name} revises {down}" for name, down in sorted(dangling.items()))
        + ". Alembic cannot build the chain, so every upgrade fails — usually the predecessor was "
        "renumbered or deleted without updating what follows it."
    )


def test_exactly_one_migration_is_the_base(down_revision_by_file: dict[str, str | None]) -> None:
    bases = sorted(name for name, down in down_revision_by_file.items() if down is None)
    assert len(bases) == 1, (
        f"Expected exactly one migration with `down_revision = None`, found {bases}. A second base "
        "starts a disconnected chain that no upgrade from the first one will ever reach."
    )


def test_the_revision_graph_has_exactly_one_head(
    script_directory: ScriptDirectory, revision_by_file: dict[str, str]
) -> None:
    # Alembic reports a duplicated id once per file that declared it; that shape is
    # test_each_revision_id_is_declared_by_exactly_one_file's to report, not this one's.
    heads = set(script_directory.get_heads())
    described = ", ".join(f"{head} ({_files_declaring(head, revision_by_file)})" for head in sorted(heads))
    assert len(heads) == 1, (
        f"Multiple migration heads: {described}. `alembic upgrade head` refuses to run against more than "
        "one head, and haku/console/database_migrate.py upgrades at console startup, so the console will "
        f"not boot. {_RENUMBER}"
    )


def test_each_file_is_named_for_the_revision_it_declares(revision_by_file: dict[str, str]) -> None:
    """Alembic ignores filenames, but the next author picks the next number by reading this directory."""
    misnamed = {name: revision for name, revision in revision_by_file.items() if not name.startswith(f"{revision}_")}
    assert not misnamed, (
        "A migration's filename must begin with the revision id it declares: "
        + "; ".join(f"{name} declares {revision}" for name, revision in sorted(misnamed.items()))
        + ". Alembic is happy either way, so this passes silently and leaves the number in the filename "
        "looking taken while the id it stands for is free (or the reverse) — which is how the next "
        "migration ends up colliding with this one."
    )


if __name__ == "__main__":
    pytest_bazel.main()
