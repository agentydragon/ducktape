"""Tests for devinfra.buildbuddy.bbr.

# TODO: Consider adding integration tests that run real `bb remote` on a small
# target and verify metadata (ROLE, TAGS) lands on BuildBuddy via bbapi. These
# would be E2E tests requiring BuildBuddy API access — not worth mocking since
# the value is in verifying the real pipeline. Manual verification done
# 2026-04-12 (invocation 5a95cd6f showed Role=claude-code, Tags=session:...).
"""

import os
import subprocess
import sys
import uuid
from pathlib import Path

import pygit2
import pytest
import pytest_bazel

from devinfra.buildbuddy.bbr import (
    _STALE_BASE_ERROR_THRESHOLD,
    _bazelrc_args,
    _bb_environment,
    _extract_invocation_id_file,
    build_command,
    check_base_branch_freshness,
    check_remote_fetch_url,
    main,
)

BB = "/usr/bin/bb"


def _make_repo(tmp_path: Path) -> pygit2.Repository:
    """Create a git repo with an initial commit and an origin/devel ref."""
    repo = pygit2.init_repository(str(tmp_path / "repo"))
    sig = pygit2.Signature("test", "test@test.com")
    tree = repo.TreeBuilder().write()
    oid = repo.create_commit("refs/heads/devel", sig, sig, "init", tree, [])
    repo.references.create("refs/remotes/origin/devel", oid)
    repo.create_reference_symbolic("refs/remotes/origin/HEAD", "refs/remotes/origin/devel", False)
    repo.set_head("refs/heads/devel")
    return repo


@pytest.mark.parametrize(
    "url", ["file:///tmp/repo", "FILE:///tmp/repo", "/tmp/repo", "../repo", "repo", "./repo:copy", "/tmp/repo:copy"]
)
def test_local_remote_is_rejected(tmp_path: Path, url: str) -> None:
    repo = _make_repo(tmp_path)
    repo.remotes.create("origin", url)
    error = check_remote_fetch_url(repo)
    assert error is not None
    assert "hosted runner cannot fetch" in error


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/repo.git",
        "ssh://git@example.com:2222/owner/repo.git",
        "git://example.com/repo.git",
        "git@example.com:owner/repo.git",
        "example.com:repo.git",
        "git@[2001:db8::1]:owner/repo.git",
        "ssh://git@[2001:db8::1]:2222/owner/repo.git",
    ],
)
def test_network_remote_is_accepted(tmp_path: Path, url: str) -> None:
    repo = _make_repo(tmp_path)
    repo.remotes.create("origin", url)
    assert check_remote_fetch_url(repo) is None


def test_selected_remote_overrides_local_origin(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    repo.remotes.create("origin", "file:///tmp/repo")
    repo.remotes.create("network", "https://example.com/repo.git")
    repo.config["buildbuddy.remote-bazel-remote-name"] = "network"
    assert check_remote_fetch_url(repo) is None


def test_local_instead_of_rewrite_is_rejected(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    repo.remotes.create("origin", "https://example.com/repo.git")
    repo.config["url.file:///tmp/mirror/.insteadOf"] = "https://example.com/"
    assert check_remote_fetch_url(repo) is not None


def test_local_remote_fails_before_invocation_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _make_repo(tmp_path)
    repo.remotes.create("origin", "file:///tmp/repo")
    monkeypatch.chdir(Path(repo.workdir))
    monkeypatch.setattr(sys, "argv", ["bbr", "test", "//foo"])

    def fail_find_bb() -> str:
        raise AssertionError("must reject the remote before preparing a BuildBuddy invocation")

    monkeypatch.setattr("devinfra.buildbuddy.bbr._find_bb", fail_find_bb)
    with pytest.raises(SystemExit, match="1"):
        main()
    assert "bbr: invocation" not in capsys.readouterr().err


class TestBazelrcArgs:
    def test_skips_comments_and_blanks(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        rc = tmp_path / "bbr.bazelrc"
        rc.write_text("# comment\n\nbuild --config=rbe\n  \n# another comment\n")
        monkeypatch.setenv("BBR_BAZELRC", str(rc))
        assert _bazelrc_args() == ["--config=rbe"]

    def test_strips_command_prefixes(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        rc = tmp_path / "bbr.bazelrc"
        rc.write_text("common --remote_cache_compression\ntest --test_output=errors\n")
        monkeypatch.setenv("BBR_BAZELRC", str(rc))
        assert _bazelrc_args() == ["--remote_cache_compression", "--test_output=errors"]

    def test_missing_file_returns_empty(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BBR_BAZELRC", str(tmp_path / "nonexistent"))
        assert _bazelrc_args() == []

    def test_prefix_only_line_skipped(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A line with just a command prefix and no flag is skipped."""
        rc = tmp_path / "bbr.bazelrc"
        rc.write_text("build\nbuild --config=rbe\n")
        monkeypatch.setenv("BBR_BAZELRC", str(rc))
        assert _bazelrc_args() == ["--config=rbe"]


class TestBuildCommand:
    """Golden command line tests for build_command().

    Each test checks the full assembled command line against the expected
    output, ensuring argument ordering is correct across all config layers.
    The minted invocation ID is opaque, so goldens splice in the returned one.
    """

    def _build(
        self, user_args: list[str], monkeypatch: pytest.MonkeyPatch, *, env: dict[str, str] | None = None
    ) -> tuple[list[str], str | None]:
        monkeypatch.setattr("devinfra.buildbuddy.bbr._find_bb", lambda: BB)
        # Clear env vars that affect command construction
        monkeypatch.delenv("BBR_BAZELRC", raising=False)
        for k, v in (env or {}).items():
            monkeypatch.setenv(k, v)
        return build_command(user_args)

    def test_basic_test(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        cmd, inv_id = self._build(["test", "//foo:bar"], monkeypatch)
        assert cmd == [BB, "remote", "test", f"--invocation_id={inv_id}", "//foo:bar"]

    def test_each_run_mints_fresh_id(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("devinfra.buildbuddy.bbr._find_bb", lambda: BB)
        _, first = build_command(["test", "//foo"])
        _, second = build_command(["test", "//foo"])
        assert first != second

    def test_user_supplied_invocation_id_adopted(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """An explicit --invocation_id is reported as-is, not overridden by a minted one."""
        user_id = "12345678-1234-1234-1234-123456789abc"
        cmd, inv_id = self._build(["test", f"--invocation_id={user_id}", "//foo"], monkeypatch)
        assert inv_id == user_id
        assert [arg for arg in cmd if "--invocation_id" in arg] == [f"--invocation_id={user_id}"]

    def test_user_supplied_invocation_id_space_form(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        user_id = "12345678-1234-1234-1234-123456789abc"
        cmd, inv_id = self._build(["test", "--invocation_id", user_id, "//foo"], monkeypatch)
        assert inv_id == user_id
        assert [arg for arg in cmd if "--invocation_id" in arg] == ["--invocation_id"]

    def test_basic_build_with_user_override(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        cmd, inv_id = self._build(["build", "--config=nolint", "//foo"], monkeypatch)
        assert cmd == [BB, "remote", "build", f"--invocation_id={inv_id}", "--config=nolint", "//foo"]

    def test_session_bazelrc(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        rc = tmp_path / "bbr.bazelrc"
        rc.write_text("build --build_metadata=TAGS=session:xyz\nbuild --build_metadata=ROLE=agent\n")
        cmd, inv_id = self._build(["build", "//foo"], monkeypatch, env={"BBR_BAZELRC": str(rc)})
        assert cmd == [
            BB,
            "remote",
            "build",
            f"--invocation_id={inv_id}",
            "--build_metadata=TAGS=session:xyz",
            "--build_metadata=ROLE=agent",
            "//foo",
        ]

    def test_remote_flags_before_verb(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        cmd, inv_id = self._build(["--timeout=10m", "test", "//foo"], monkeypatch)
        assert cmd == [BB, "remote", "--timeout=10m", "test", f"--invocation_id={inv_id}", "//foo"]

    def test_all_layers(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Remote flags precede the verb; session metadata precedes user Bazel flags."""
        rc = tmp_path / "bbr.bazelrc"
        rc.write_text("build --build_metadata=TAGS=session:s1\n")
        cmd, inv_id = self._build(
            ["--timeout=10m", "test", "//foo:bar", "--nocache_test_results"], monkeypatch, env={"BBR_BAZELRC": str(rc)}
        )
        assert cmd == [
            BB,
            "remote",
            "--timeout=10m",
            "test",
            f"--invocation_id={inv_id}",
            "--build_metadata=TAGS=session:s1",
            "//foo:bar",
            "--nocache_test_results",
        ]

    def test_startup_options_before_verb(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        cmd, inv_id = self._build(["--output_base=/tmp/bazel", "build", "//foo"], monkeypatch)
        assert cmd == [BB, "remote", "--output_base=/tmp/bazel", "build", f"--invocation_id={inv_id}", "//foo"]

    def test_no_verb(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """When no verb is found, everything is startup options and no ID is minted."""
        cmd, inv_id = self._build(["--flag", "//foo"], monkeypatch)
        assert inv_id is None
        assert cmd == [BB, "remote", "--flag", "//foo"]


class TestExtractInvocationIdFile:
    def test_absent(self) -> None:
        assert _extract_invocation_id_file(["test", "//foo"]) == (["test", "//foo"], None)

    def test_stripped_anywhere(self) -> None:
        """bbr's own flag is recognized before or after the verb, like --dry-run."""
        args, path = _extract_invocation_id_file(["test", "--invocation-id-file=/tmp/inv", "//foo"])
        assert args == ["test", "//foo"]
        assert path == Path("/tmp/inv")

    def test_last_wins(self) -> None:
        args, path = _extract_invocation_id_file(
            ["--invocation-id-file=/tmp/a", "test", "--invocation-id-file=/tmp/b", "//foo"]
        )
        assert args == ["test", "//foo"]
        assert path == Path("/tmp/b")


def test_mounted_key_only_forwarded_in_child_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    key_file = tmp_path / "api-key"
    key_file.write_text("fake-test-key\n")
    monkeypatch.setenv("BBR_BUILDBUDDY_API_KEY_FILE", str(key_file))
    monkeypatch.delenv("BUILDBUDDY_API_KEY", raising=False)
    env = _bb_environment()
    assert env is not None
    assert env["BUILDBUDDY_API_KEY"] == "fake-test-key"
    assert "BUILDBUDDY_API_KEY" not in os.environ
    monkeypatch.setenv("BUILDBUDDY_API_KEY", "existing-key")
    assert _bb_environment() is None  # explicit environment wins


def test_missing_key_file_is_not_read_for_dry_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _make_repo(tmp_path)
    monkeypatch.setattr("devinfra.buildbuddy.bbr._find_bb", lambda: BB)
    monkeypatch.chdir(Path(repo.workdir))
    monkeypatch.setattr(sys, "argv", ["bbr", "--dry-run", "build", "//foo"])
    monkeypatch.setenv("BBR_BUILDBUDDY_API_KEY_FILE", str(tmp_path / "missing"))
    main()  # dry-run must not read the key file


class TestMain:
    def _setup(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, argv: list[str]) -> None:
        repo = _make_repo(tmp_path)
        monkeypatch.setattr("devinfra.buildbuddy.bbr._find_bb", lambda: BB)
        monkeypatch.delenv("BBR_BAZELRC", raising=False)
        monkeypatch.chdir(Path(repo.workdir))
        monkeypatch.setattr(sys, "argv", ["bbr", *argv])

    def test_id_printed_and_recorded_before_run(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """An interrupted run must still leave the ID on stderr and in the file."""
        id_file = tmp_path / "inv_id"
        at_run_start: dict[str, str] = {}

        def fake_run(cmd: list[str], check: bool) -> subprocess.CompletedProcess[bytes]:
            at_run_start["stderr"] = capsys.readouterr().err
            at_run_start["file"] = id_file.read_text()
            return subprocess.CompletedProcess(cmd, 17)

        monkeypatch.setattr("devinfra.buildbuddy.bbr.subprocess.run", fake_run)
        self._setup(tmp_path, monkeypatch, [f"--invocation-id-file={id_file}", "test", "//foo"])
        with pytest.raises(SystemExit) as excinfo:
            main()

        assert excinfo.value.code == 17  # bb remote's exit code is propagated
        inv_id = at_run_start["file"]
        uuid.UUID(inv_id)  # Bazel rejects a non-UUID --invocation_id, so the minted ID must be one
        assert f"bbr: invocation {inv_id}" in at_run_start["stderr"]
        # Post-run summary repeats the ID with bbapi recipes.
        post_run = capsys.readouterr().err
        assert inv_id in post_run
        assert "bbapi" in post_run

    def test_key_file_passed_only_to_bb_child(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        self._setup(tmp_path, monkeypatch, ["build", "//foo"])
        key_file = tmp_path / "api-key"
        key_file.write_text("fake-test-key\n")
        monkeypatch.setenv("BBR_BUILDBUDDY_API_KEY_FILE", str(key_file))
        monkeypatch.delenv("BUILDBUDDY_API_KEY", raising=False)
        received = []

        def fake_run(cmd: list[str], check: bool, env: dict[str, str]) -> subprocess.CompletedProcess[bytes]:
            received.append(env["BUILDBUDDY_API_KEY"])
            return subprocess.CompletedProcess(cmd, 0)

        monkeypatch.setattr("devinfra.buildbuddy.bbr.subprocess.run", fake_run)
        with pytest.raises(SystemExit) as excinfo:
            main()
        assert excinfo.value.code == 0
        assert received == ["fake-test-key"]
        assert "fake-test-key" not in capsys.readouterr().err

    def test_invocation_id_file_without_verb_errors(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """No verb means no ID to record — refuse instead of silently skipping the write."""

        def fail_run(cmd: list[str], check: bool) -> subprocess.CompletedProcess[bytes]:
            raise AssertionError("bb remote must not run")

        monkeypatch.setattr("devinfra.buildbuddy.bbr.subprocess.run", fail_run)
        id_file = tmp_path / "inv_id"
        self._setup(tmp_path, monkeypatch, [f"--invocation-id-file={id_file}", "--flag", "//foo"])
        with pytest.raises(SystemExit) as excinfo:
            main()

        assert excinfo.value.code == 1
        assert "--invocation-id-file" in capsys.readouterr().err
        assert not id_file.exists()


def _commit(repo: pygit2.Repository, ref: str, message: str, parents: list) -> pygit2.Oid:
    sig = pygit2.Signature("test", "test@test.com")
    tree = repo.TreeBuilder().write()
    return repo.create_commit(ref, sig, sig, message, tree, parents)


def _repo_on_branch(tmp_path: Path, branch: str) -> tuple[pygit2.Repository, pygit2.Oid]:
    """A fresh repo with one commit on `branch`, checked out."""
    repo = pygit2.init_repository(str(tmp_path / "repo"))
    base = _commit(repo, f"refs/heads/{branch}", "init", [])
    repo.set_head(f"refs/heads/{branch}")
    return repo, base


class TestCheckBaseBranchFreshness:
    """`check_base_branch_freshness` is a no-network sanity check on bb
    remote's likely `<default-branch>@{upstream}` diff base (see
    devinfra/buildbuddy/docs/remote.md) — it never fetches, only reports (main() turns a report into a hard error).
    """

    def test_no_bb_config_returns_none(self, tmp_path: Path) -> None:
        repo, _base = _repo_on_branch(tmp_path, "devel")
        assert check_base_branch_freshness(repo) is None

    def test_current_branch_tracked_on_remote_returns_none(self, tmp_path: Path) -> None:
        repo, base = _repo_on_branch(tmp_path, "feature")
        repo.references.create("refs/remotes/origin/feature", base)
        repo.config["buildbuddy.remote-bazel-default-branch"] = "devel"
        assert check_base_branch_freshness(repo) is None

    def test_current_branch_tracked_but_ahead_falls_through_to_default_branch_check(self, tmp_path: Path) -> None:
        """Unpushed commits on a tracked branch still fall back to `<default>@{upstream}`.

        Mirrors bb's own Phase 2 logic (bb_remote_internals.md): HEAD must be
        an ancestor of (or equal to) the tracked commit for bb to use HEAD
        directly — merely having a tracking ref isn't enough.
        """
        repo, base = _repo_on_branch(tmp_path, "feature")
        repo.references.create("refs/remotes/origin/feature", base)  # tracked, but about to fall behind HEAD
        repo.references.create("refs/remotes/origin/devel", base)
        parent = _commit(repo, "refs/heads/feature", "unpushed work", [base])
        repo.config["buildbuddy.remote-bazel-default-branch"] = "devel"

        # devel's tracking ref is fresh (still at base, 1 commit behind HEAD) — no warning.
        assert check_base_branch_freshness(repo) is None

        # Now make devel's tracking ref stale too — this time it should warn,
        # proving the "tracked feature branch" check didn't short-circuit.
        for i in range(_STALE_BASE_ERROR_THRESHOLD + 5):
            parent = _commit(repo, "refs/heads/feature", f"c{i}", [parent])
        warning = check_base_branch_freshness(repo)
        assert warning is not None
        assert "origin/devel" in warning

    def test_fresh_tracking_ref_returns_none(self, tmp_path: Path) -> None:
        repo, base = _repo_on_branch(tmp_path, "feature")
        repo.references.create("refs/remotes/origin/devel", base)
        _commit(repo, "refs/heads/feature", "work", [base])
        repo.config["buildbuddy.remote-bazel-default-branch"] = "devel"
        assert check_base_branch_freshness(repo) is None

    def test_stale_tracking_ref_reports(self, tmp_path: Path) -> None:
        repo, base = _repo_on_branch(tmp_path, "feature")
        repo.references.create("refs/remotes/origin/devel", base)
        parent = base
        for i in range(_STALE_BASE_ERROR_THRESHOLD + 5):
            parent = _commit(repo, "refs/heads/feature", f"c{i}", [parent])
        repo.config["buildbuddy.remote-bazel-default-branch"] = "devel"

        warning = check_base_branch_freshness(repo)

        assert warning is not None
        assert "origin/devel" in warning
        assert "git fetch origin devel" in warning
        assert "BBR_ALLOW_STALE_BASE" in warning
        assert "feature" in warning  # names the current branch as fetchable base

    def test_respects_configured_remote_and_branch(self, tmp_path: Path) -> None:
        repo, base = _repo_on_branch(tmp_path, "feature")
        repo.references.create("refs/remotes/upstream/main", base)
        parent = base
        for i in range(_STALE_BASE_ERROR_THRESHOLD + 5):
            parent = _commit(repo, "refs/heads/feature", f"c{i}", [parent])
        repo.config["buildbuddy.remote-bazel-remote-name"] = "upstream"
        repo.config["buildbuddy.remote-bazel-default-branch"] = "main"

        warning = check_base_branch_freshness(repo)

        assert warning is not None
        assert "upstream/main" in warning

    def test_missing_tracking_ref_returns_none(self, tmp_path: Path) -> None:
        repo, _base = _repo_on_branch(tmp_path, "feature")
        repo.config["buildbuddy.remote-bazel-default-branch"] = "devel"
        # No refs/remotes/origin/devel at all — nothing to compare against.
        assert check_base_branch_freshness(repo) is None

    def test_detached_head_returns_none(self, tmp_path: Path) -> None:
        repo, base = _repo_on_branch(tmp_path, "devel")
        repo.references.create("refs/remotes/origin/devel", base)
        repo.config["buildbuddy.remote-bazel-default-branch"] = "devel"
        repo.set_head(base)
        assert check_base_branch_freshness(repo) is None


if __name__ == "__main__":
    pytest_bazel.main()
