import json
import os
import sqlite3
import subprocess
import sys
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

import pytest
import pytest_bazel
from python.runfiles import Runfiles


@dataclass
class Harness:
    home: Path
    claude: Path
    codex: Path
    staging: Path
    environment: dict[str, str]
    runfiles: Runfiles

    def restic(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [self.runfiles.root() / "ducktape/third_party/restic/restic_bin", *arguments],
            env=self.environment,
            check=True,
            capture_output=True,
            text=True,
        )

    def backup(self, *, check: bool = True) -> subprocess.CompletedProcess[str]:
        runfiles_root = self.runfiles.root()
        return subprocess.run(
            [
                sys.executable,
                (runfiles_root / "ducktape/nix/home/services/session_backup.py"),
                (runfiles_root / "ducktape/third_party/restic/restic_bin"),
                (runfiles_root / "ducktape/nix/home/services/snapshot_sqlite.py"),
                str(self.home),
                str(self.claude),
                str(self.codex),
                str(self.staging),
                "fixture-host",
            ],
            env=self.environment,
            check=check,
            capture_output=True,
            text=True,
        )

    def snapshot_ids(self) -> list[str]:
        return [str(snapshot["id"]) for snapshot in json.loads(self.restic("snapshots", "--json").stdout)]


@pytest.fixture
def harness(tmp_path: Path, runfiles: Runfiles) -> Harness:
    home = tmp_path / "home"
    claude, codex = home / ".claude", home / ".codex"
    claude.mkdir(parents=True)
    codex.mkdir()
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    password = tmp_path / "password"
    password.write_text("disposable-fixture-only\n")
    environment = os.environ | {
        "RESTIC_REPOSITORY": str(tmp_path / "repository"),
        "RESTIC_PASSWORD_FILE": str(password),
        "RESTIC_CACHE_DIR": str(tmp_path / "cache"),
        "RUNTIME_DIRECTORY": str(runtime),
    }
    environment.pop("RESTIC_PASSWORD", None)
    return Harness(home, claude, codex, home / ".local/state/ducktape/ai-session-backup-sqlite", environment, runfiles)


def test_restores_raw_tree_and_live_wal_database(harness: Harness, tmp_path: Path) -> None:
    files = {
        harness.claude / ".credentials.json": b'{"fixture": "claude"}',
        harness.claude / "projects/session/subagents/agent.jsonl": b'{"tool_result":"retained"}\n',
        harness.codex / "auth.json": b'{"fixture": "codex"}',
        harness.codex / "config.toml": b'model = "fixture"\n',
    }
    for path, content in files.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    (harness.claude / "link").symlink_to(".credentials.json")
    database = harness.codex / "state_5.sqlite"
    with closing(sqlite3.connect(database)) as writer, writer:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("CREATE TABLE records (value TEXT)")
        writer.execute("INSERT INTO records VALUES ('committed-in-wal')")
        writer.commit()
        assert Path(str(database) + "-wal").stat().st_size > 0

        harness.backup()
        restored = tmp_path / "restored"
        harness.restic("restore", harness.snapshot_ids()[0], "--target", str(restored))
        for path, content in files.items():
            assert (restored / path.relative_to(harness.home)).read_bytes() == content
        assert (restored / ".claude/link").is_symlink()
        assert (restored / ".claude/link").readlink() == Path(".credentials.json")
        assert (restored / ".codex/state_5.sqlite").is_file()
        assert (restored / ".codex/state_5.sqlite-wal").is_file()
        restored_staging = restored / harness.staging.relative_to(harness.home)
        manifest = json.loads((restored_staging / "manifest.json").read_text())
        entry = next(entry for entry in manifest["databases"] if entry["source"] == str(database))
        with closing(sqlite3.connect(restored_staging / entry["image"])) as image, image:
            assert image.execute("PRAGMA integrity_check").fetchone() == ("ok",)
            assert image.execute("SELECT value FROM records").fetchone() == ("committed-in-wal",)


def test_idle_capture_skips_snapshot_and_gc_keeps_history(harness: Harness, tmp_path: Path) -> None:
    session = harness.claude / "session.jsonl"
    session.write_text('{"message":"original"}\n')
    with closing(sqlite3.connect(harness.codex / "memories_1.sqlite")) as database, database:
        database.execute("CREATE TABLE records (value TEXT)")
        database.execute("INSERT INTO records VALUES ('remembered')")
    harness.backup()
    first = harness.snapshot_ids()[0]
    harness.backup()
    assert harness.snapshot_ids() == [first]

    session.unlink()
    harness.backup()
    assert len(harness.snapshot_ids()) == 2
    restored = tmp_path / "historical"
    harness.restic("restore", first, "--target", str(restored))
    assert (restored / ".claude/session.jsonl").read_text() == '{"message":"original"}\n'


def test_bad_database_reports_failure_but_still_archives_raw_data(harness: Harness, tmp_path: Path) -> None:
    damaged = b"SQLite format 3\x00" + b"damaged" * 128
    (harness.codex / "state_5.sqlite").write_bytes(damaged)
    session = harness.claude / "session.jsonl"
    session.write_text('{"message":"still captured"}\n')
    result = harness.backup(check=False)
    assert result.returncode == 1
    assert "SQLite snapshot failed" in result.stderr

    restored = tmp_path / "partial"
    harness.restic("restore", harness.snapshot_ids()[0], "--target", str(restored))
    assert (restored / ".codex/state_5.sqlite").read_bytes() == damaged
    assert (restored / ".claude/session.jsonl").read_bytes() == session.read_bytes()
    manifest = json.loads((restored / harness.staging.relative_to(harness.home) / "manifest.json").read_text())
    assert len(manifest["errors"]) == 1
    assert manifest["databases"] == []


def test_excludes_disposable_files_and_previously_staged_images(harness: Harness, tmp_path: Path) -> None:
    directories = {
        "claude": ["session-env", ".tmp", "cache", ".cache", "plugins/cache", "debug", "logs"],
        "codex": ["worktrees", ".tmp", "tmp", "packages", "cache", ".cache", "plugins/cache", "log", "logs"],
    }
    for name, root in (("claude", harness.claude), ("codex", harness.codex)):
        for directory in directories[name]:
            source = root / directory / "nested.db"
            source.parent.mkdir(parents=True, exist_ok=True)
            with closing(sqlite3.connect(source)) as database, database:
                database.execute("CREATE TABLE unwanted (value TEXT)")
            stale = harness.staging / "images" / name / "old-root" / directory / "nested.db"
            stale.parent.mkdir(parents=True, exist_ok=True)
            stale.write_bytes(b"old staged image")

    diagnostic_db = harness.codex / "logs_2.sqlite"
    with closing(sqlite3.connect(diagnostic_db)) as database, database:
        database.execute("CREATE TABLE logs (value TEXT)")
    (harness.codex / "logs_2.sqlite-wal").write_bytes(b"old wal")
    stale_log = harness.staging / "images/codex/old-root/logs_2.sqlite"
    stale_log.write_bytes(b"old staged log")
    discarded = [
        harness.claude / "stats-cache.json",
        harness.claude / "mcp-needs-auth-cache.json",
        harness.claude / "plugins/plugin-directory-cache-v2.json",
        harness.claude / "daemon.log",
        harness.codex / "models_cache.json",
    ]
    for path in discarded:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("discard")
    memory = harness.codex / "memories_1.sqlite"
    with closing(sqlite3.connect(memory)) as database, database:
        database.execute("CREATE TABLE memories (value TEXT)")
    session = harness.claude / "projects/example/session.jsonl"
    session.parent.mkdir(parents=True)
    session.write_text('{"message":"keep"}\n')

    harness.backup()
    restored = tmp_path / "restored"
    harness.restic("restore", harness.snapshot_ids()[0], "--target", str(restored))
    for name, root in (("claude", harness.claude), ("codex", harness.codex)):
        for directory in directories[name]:
            assert not (restored / (root / directory).relative_to(harness.home)).exists()
            assert not (
                restored / harness.staging.relative_to(harness.home) / "images" / name / "old-root" / directory
            ).exists()
    assert not (restored / ".codex/logs_2.sqlite").exists()
    assert not (restored / ".codex/logs_2.sqlite-wal").exists()
    assert not (restored / harness.staging.relative_to(harness.home) / "images/codex/old-root/logs_2.sqlite").exists()
    assert all(not (restored / path.relative_to(harness.home)).exists() for path in discarded)
    assert (restored / session.relative_to(harness.home)).read_text() == session.read_text()
    assert (restored / memory.relative_to(harness.home)).is_file()
    manifest = json.loads((restored / harness.staging.relative_to(harness.home) / "manifest.json").read_text())
    assert [entry["source"] for entry in manifest["databases"]] == [str(memory)]


def test_excludes_thread_history_projection_but_preserves_journals_and_memory(harness: Harness, tmp_path: Path) -> None:
    projection = harness.codex / "thread_history_1.sqlite"
    journal = harness.codex / "sessions/2026/rollout.jsonl"
    journal.parent.mkdir(parents=True)
    journal.write_text('{"message":"keep"}\n')
    memory = harness.codex / "memories_1.sqlite"
    with closing(sqlite3.connect(memory)) as database, database:
        database.execute("CREATE TABLE memories (value TEXT)")
        database.execute("INSERT INTO memories VALUES ('remembered')")
    stale = harness.staging / "images/codex/old-root/thread_history_1.sqlite"
    stale.parent.mkdir(parents=True)
    for suffix in ("", "-wal", "-shm", "-journal"):
        Path(str(stale) + suffix).write_bytes(b"previously staged projection")

    with closing(sqlite3.connect(projection)) as writer, writer:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("CREATE TABLE projected (value TEXT)")
        writer.execute("INSERT INTO projected VALUES ('derived')")
        writer.commit()
        assert Path(str(projection) + "-wal").stat().st_size > 0
        assert Path(str(projection) + "-shm").exists()
        Path(str(projection) + "-journal").write_bytes(b"old journal")

        harness.backup()
        restored = tmp_path / "restored"
        harness.restic("restore", harness.snapshot_ids()[0], "--target", str(restored))
        for suffix in ("", "-wal", "-shm", "-journal"):
            assert not (restored / ".codex" / (projection.name + suffix)).exists()
            assert not (restored / stale.relative_to(harness.home).parent / (stale.name + suffix)).exists()
        assert (restored / journal.relative_to(harness.home)).read_text() == journal.read_text()
        assert (restored / memory.relative_to(harness.home)).exists()
        staged = restored / harness.staging.relative_to(harness.home)
        manifest = json.loads((staged / "manifest.json").read_text())
        assert [entry["source"] for entry in manifest["databases"]] == [str(memory)]
        with closing(sqlite3.connect(staged / manifest["databases"][0]["image"])) as database:
            assert database.execute("SELECT value FROM memories").fetchone() == ("remembered",)


if __name__ == "__main__":
    pytest_bazel.main()
