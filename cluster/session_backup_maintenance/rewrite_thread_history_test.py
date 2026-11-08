import importlib.util
import json
import os
import subprocess
from pathlib import Path

import pytest
import pytest_bazel
from python.runfiles import Runfiles


@pytest.fixture
def repository(tmp_path: Path, runfiles: Runfiles, monkeypatch: pytest.MonkeyPatch) -> Path:
    binary = runfiles.root() / "ducktape/third_party/restic/restic_bin"
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "restic").symlink_to(binary)
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")
    monkeypatch.setenv("RESTIC_PASSWORD", "disposable-test-password")
    monkeypatch.setenv("RESTIC_REPOSITORY", str(tmp_path / "repository"))
    monkeypatch.setenv("RESTIC_CACHE_DIR", str(tmp_path / "cache"))
    subprocess.run(["restic", "init"], check=True, capture_output=True)
    return tmp_path / "home"


def run(*args: str, cwd: Path | None = None) -> str:
    return subprocess.run(["restic", *args], cwd=cwd, check=True, text=True, stdout=subprocess.PIPE).stdout


def test_stages_and_verifies_only_projection(repository: Path, tmp_path: Path, runfiles: Runfiles) -> None:
    module_path = runfiles.root() / "ducktape/cluster/session_backup_maintenance/rewrite_thread_history.py"
    spec = importlib.util.spec_from_file_location("rewrite_thread_history", module_path)
    assert spec is not None
    assert spec.loader is not None
    rewrite = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rewrite)
    codex = repository / ".codex"
    stage = repository / ".local/state/ducktape/ai-session-backup-sqlite/images/codex/abc"
    codex.mkdir(parents=True)
    stage.mkdir(parents=True)
    (codex / "thread_history_1.sqlite").write_text("projection")
    (codex / "thread_history_1.sqlite-wal").write_text("sidecar")
    (codex / "session.jsonl").write_text("keep journal")
    (stage / "thread_history_1.sqlite").write_text("staged projection")
    (stage / "memories_1.sqlite").write_text("keep memory")
    run("backup", ".codex", ".local", cwd=repository)
    original = json.loads(run("snapshots", "--json"))[0]["id"]
    (codex / "thread_history_1.sqlite").write_text("second projection")
    run("backup", ".codex", ".local", cwd=repository)
    originals = json.loads(run("snapshots", "--json"))
    assert len(originals) == 2
    assert any(row.get("parent") == original for row in originals)
    plan = tmp_path / "plan.json"
    report = tmp_path / "report.json"
    rewrite.plan(plan)
    assert len(json.loads(run("snapshots", "--json"))) == 2
    rewrite.stage(plan)
    rewrite.stage(plan)  # An interrupted run can be resumed without duplicating replacements.
    rewrite.verify(plan, report)
    replacement = json.loads(report.read_text())["replacements"][original]
    assert len(json.loads(report.read_text())["replacements"]) == 2
    assert len(json.loads(run("snapshots", "--json"))) == 4
    assert run("dump", original, "/.codex/thread_history_1.sqlite") == "projection"
    assert run("dump", replacement, "/.codex/session.jsonl") == "keep journal"
    assert (
        run("dump", replacement, "/.local/state/ducktape/ai-session-backup-sqlite/images/codex/abc/memories_1.sqlite")
        == "keep memory"
    )
    listing = run("ls", replacement)
    assert "thread_history_1.sqlite" not in listing


def test_rejects_a_rewrite_with_unrelated_deletions(repository: Path, tmp_path: Path, runfiles: Runfiles) -> None:
    module_path = runfiles.root() / "ducktape/cluster/session_backup_maintenance/rewrite_thread_history.py"
    spec = importlib.util.spec_from_file_location("rewrite_thread_history", module_path)
    assert spec is not None
    assert spec.loader is not None
    rewrite = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rewrite)
    codex = repository / ".codex"
    codex.mkdir(parents=True)
    (codex / "thread_history_1.sqlite").write_text("projection")
    (codex / "session.jsonl").write_text("keep")
    run("backup", ".codex", cwd=repository)
    plan = tmp_path / "plan.json"
    rewrite.plan(plan)
    run("rewrite", "--exclude", "/.codex/thread_history_1.sqlite", "--exclude", "/.codex/session.jsonl")
    with pytest.raises(ValueError, match="unexpected diff"):
        rewrite.verify(plan, tmp_path / "report.json")
    assert not (tmp_path / "report.json").exists()


if __name__ == "__main__":
    pytest_bazel.main()
