import json
import os
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest_bazel

from nix.home.services import snapshot_sqlite


def create_database(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as database, database:
        database.execute("CREATE TABLE records (value TEXT)")
        database.execute("INSERT INTO records VALUES (?)", (value,))


def test_discovers_all_database_names_and_harnesses(tmp_path: Path) -> None:
    roots = {name: tmp_path / name for name in ("claude", "codex")}
    output = tmp_path / "snapshots"
    expected = {
        roots["codex"] / "state_5.sqlite": "state",
        roots["codex"] / "thread_history_1.sqlite": "history",
        roots["codex"] / "plugins/cache/nested.db": "plugin",
        roots["claude"] / "opaque-store": "claude",
    }
    for path, value in expected.items():
        create_database(path, value)

    assert snapshot_sqlite.snapshot_databases(roots, output) == []
    manifest = json.loads((output / "manifest.json").read_text())
    assert {Path(entry["source"]) for entry in manifest["databases"]} == set(expected)
    for entry in manifest["databases"]:
        image = output / entry["image"]
        with closing(sqlite3.connect(image)) as restored, restored:
            assert restored.execute("PRAGMA integrity_check").fetchone() == ("ok",)
            assert restored.execute("SELECT value FROM records").fetchone() == (expected[Path(entry["source"])],)
        assert image.stat().st_mode & 0o777 == 0o600


def test_unchanged_images_and_manifest_keep_inodes_and_timestamps(tmp_path: Path) -> None:
    root = tmp_path / "codex"
    database = root / "memories_1.sqlite"
    create_database(database, "remembered")
    output = tmp_path / "snapshots"
    assert snapshot_sqlite.snapshot_databases({"codex": root}, output) == []
    paths = [output, *output.rglob("*")]

    def identities() -> dict[Path, tuple[int, int, int]]:
        return {path: (s.st_ino, s.st_mtime_ns, s.st_ctime_ns) for path in paths for s in [path.stat()]}

    before = identities()
    assert snapshot_sqlite.snapshot_databases({"codex": root}, output) == []
    assert identities() == before


def test_missing_source_keeps_last_consistent_image(tmp_path: Path) -> None:
    root = tmp_path / "codex"
    database = root / "goals_1.sqlite"
    create_database(database, "retained")
    output = tmp_path / "snapshots"
    assert snapshot_sqlite.snapshot_databases({"codex": root}, output) == []
    manifest = json.loads((output / "manifest.json").read_text())
    image = output / manifest["databases"][0]["image"]
    database.unlink()

    assert snapshot_sqlite.snapshot_databases({"codex": root}, output) == []
    assert json.loads((output / "manifest.json").read_text())["databases"] == []
    with closing(sqlite3.connect(image)) as restored, restored:
        assert restored.execute("SELECT value FROM records").fetchone() == ("retained",)


def test_does_not_follow_external_symlinks_or_read_pipes(tmp_path: Path) -> None:
    root = tmp_path / "claude"
    root.mkdir()
    external = tmp_path / "outside.db"
    create_database(external, "outside")
    (root / "linked.db").symlink_to(external)
    pipe = root / "pipe"
    os.mkfifo(pipe)
    output = tmp_path / "snapshots"
    assert snapshot_sqlite.snapshot_databases({"claude": root}, output) == []
    assert json.loads((output / "manifest.json").read_text())["databases"] == []


if __name__ == "__main__":
    pytest_bazel.main()
