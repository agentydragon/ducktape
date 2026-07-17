"""Tests for devinfra.bbr.

# TODO: Consider adding integration tests that run real `bb remote` on a small
# target and verify metadata (ROLE, TAGS) lands on BuildBuddy via bbapi. These
# would be E2E tests requiring BuildBuddy API access — not worth mocking since
# the value is in verifying the real pipeline. Manual verification done
# 2026-04-12 (invocation 5a95cd6f showed Role=claude-code, Tags=session:...).
"""

import json
from pathlib import Path

import pygit2
import pytest
import pytest_bazel

from devinfra.bbr import (
    _STALE_BASE_ERROR_THRESHOLD,
    RepoConfig,
    _bazelrc_args,
    _env_args,
    _read_repo_config,
    build_command,
    check_base_branch_freshness,
    find_verb_index,
)

BB = "/usr/bin/bb"
IMAGE = "ghcr.io/test/rbe@sha256:deadbeef"


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


def _setup_repo_config(repo: pygit2.Repository, config: dict | None = None) -> None:
    """Write devinfra/bbr.json into a test repo."""
    repo_root = Path(repo.workdir)
    devinfra = repo_root / "devinfra"
    devinfra.mkdir(exist_ok=True)

    if config is None:
        config = {
            "runner_exec_properties": {"workload-isolation-type": "firecracker"},
            "container_image": IMAGE,
            "bazel_args": ["--config=rbe"],
        }
    (devinfra / "bbr.json").write_text(json.dumps(config))


def _inv_id_file() -> str:
    return f"--invocation_id_file={Path.home() / '.cache/bbr/last_invocation_id'}"


class TestReadRepoConfig:
    def test_reads_config(self, tmp_path: Path) -> None:
        devinfra = tmp_path / "devinfra"
        devinfra.mkdir()
        config = {
            "runner_exec_properties": {"EstimatedComputeUnits": "8"},
            "container_image": "ghcr.io/test/rbe@sha256:abc",
            "bazel_args": ["--config=rbe", "--config=nolint"],
        }
        (devinfra / "bbr.json").write_text(json.dumps(config))
        result = _read_repo_config(tmp_path)
        assert result.runner_exec_properties == {"EstimatedComputeUnits": "8"}
        assert result.container_image == "ghcr.io/test/rbe@sha256:abc"
        assert result.bazel_args == ["--config=rbe", "--config=nolint"]

    def test_missing_file_returns_defaults(self, tmp_path: Path) -> None:
        result = _read_repo_config(tmp_path)
        assert result == RepoConfig()

    def test_partial_config(self, tmp_path: Path) -> None:
        devinfra = tmp_path / "devinfra"
        devinfra.mkdir()
        (devinfra / "bbr.json").write_text('{"bazel_args": ["--config=rbe"]}')
        result = _read_repo_config(tmp_path)
        assert result.bazel_args == ["--config=rbe"]
        assert result.runner_exec_properties == {}
        assert result.container_image is None


class TestFindVerbIndex:
    def test_verb_first(self) -> None:
        assert find_verb_index(["test", "//foo"]) == 0

    def test_verb_after_flags(self) -> None:
        assert find_verb_index(["--some-flag", "build", "//foo"]) == 1

    def test_no_verb(self) -> None:
        assert find_verb_index(["--flag", "//foo"]) is None

    def test_short_flag_before_verb(self) -> None:
        assert find_verb_index(["-k", "test", "//foo"]) == 1

    def test_run(self) -> None:
        assert find_verb_index(["run", "//foo:bar"]) == 0

    def test_query(self) -> None:
        assert find_verb_index(["query", "deps(//foo)"]) == 0

    def test_empty(self) -> None:
        assert find_verb_index([]) is None


class TestEnvArgs:
    def test_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("BBR_REMOTE_ARGS", raising=False)
        assert _env_args("BBR_REMOTE_ARGS") == []

    def test_single_flag(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BBR_REMOTE_ARGS", "--timeout=300")
        assert _env_args("BBR_REMOTE_ARGS") == ["--timeout=300"]

    def test_multiple_flags(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BBR_REMOTE_ARGS", "--timeout=300 --os=linux")
        assert _env_args("BBR_REMOTE_ARGS") == ["--timeout=300", "--os=linux"]

    def test_quoted_value(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BBR_REMOTE_ARGS", '--env="FOO=bar baz"')
        assert _env_args("BBR_REMOTE_ARGS") == ["--env=FOO=bar baz"]


class TestBazelrcArgs:
    def test_reads_flags(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        rc = tmp_path / "bbr.bazelrc"
        rc.write_text("build --config=rbe\nbuild --build_metadata=TAGS=session:abc\n")
        monkeypatch.setenv("BBR_BAZELRC", str(rc))
        assert _bazelrc_args() == ["--config=rbe", "--build_metadata=TAGS=session:abc"]

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

    def test_unset_env_returns_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("BBR_BAZELRC", raising=False)
        assert _bazelrc_args() == []

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
    """

    def _build(
        self,
        tmp_path: Path,
        user_args: list[str],
        monkeypatch: pytest.MonkeyPatch,
        *,
        env: dict[str, str] | None = None,
    ) -> list[str]:
        repo = _make_repo(tmp_path)
        _setup_repo_config(repo)
        monkeypatch.setattr("devinfra.bbr._find_bb", lambda: BB)
        # Clear env vars that affect command construction
        for var in ("BBR_REMOTE_ARGS", "BBR_BAZELRC"):
            monkeypatch.delenv(var, raising=False)
        for k, v in (env or {}).items():
            monkeypatch.setenv(k, v)
        return build_command(repo, user_args)

    def test_basic_test(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        cmd = self._build(tmp_path, ["test", "//foo:bar"], monkeypatch)
        assert cmd == [
            BB,
            "remote",
            _inv_id_file(),
            "--runner_exec_properties=workload-isolation-type=firecracker",
            f"--container_image=docker://{IMAGE}",
            "test",
            "--config=rbe",
            "//foo:bar",
        ]

    def test_basic_build_with_user_override(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        cmd = self._build(tmp_path, ["build", "--config=nolint", "//foo"], monkeypatch)
        assert cmd == [
            BB,
            "remote",
            _inv_id_file(),
            "--runner_exec_properties=workload-isolation-type=firecracker",
            f"--container_image=docker://{IMAGE}",
            "build",
            "--config=rbe",
            "--config=nolint",
            "//foo",
        ]

    def test_session_bazelrc(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        rc = tmp_path / "bbr.bazelrc"
        rc.write_text("build --build_metadata=TAGS=session:xyz\nbuild --build_metadata=ROLE=agent\n")
        cmd = self._build(tmp_path, ["build", "//foo"], monkeypatch, env={"BBR_BAZELRC": str(rc)})
        assert cmd == [
            BB,
            "remote",
            _inv_id_file(),
            "--runner_exec_properties=workload-isolation-type=firecracker",
            f"--container_image=docker://{IMAGE}",
            "build",
            "--config=rbe",
            "--build_metadata=TAGS=session:xyz",
            "--build_metadata=ROLE=agent",
            "//foo",
        ]

    def test_bbr_remote_args_in_slot2(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        cmd = self._build(tmp_path, ["test", "//foo"], monkeypatch, env={"BBR_REMOTE_ARGS": "--timeout=600"})
        assert cmd == [
            BB,
            "remote",
            _inv_id_file(),
            "--runner_exec_properties=workload-isolation-type=firecracker",
            f"--container_image=docker://{IMAGE}",
            "--timeout=600",
            "test",
            "--config=rbe",
            "//foo",
        ]

    def test_all_layers(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """All config layers: repo + session bazelrc + BBR_REMOTE_ARGS + user flags."""
        rc = tmp_path / "bbr.bazelrc"
        rc.write_text("build --build_metadata=TAGS=session:s1\n")
        cmd = self._build(
            tmp_path,
            ["test", "//foo:bar", "--nocache_test_results"],
            monkeypatch,
            env={"BBR_BAZELRC": str(rc), "BBR_REMOTE_ARGS": "--timeout=600"},
        )
        assert cmd == [
            BB,
            "remote",
            _inv_id_file(),
            "--runner_exec_properties=workload-isolation-type=firecracker",
            f"--container_image=docker://{IMAGE}",
            "--timeout=600",
            "test",
            "--config=rbe",
            "--build_metadata=TAGS=session:s1",
            "//foo:bar",
            "--nocache_test_results",
        ]

    def test_startup_options_before_verb(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        cmd = self._build(tmp_path, ["--output_base=/tmp/bazel", "build", "//foo"], monkeypatch)
        assert cmd == [
            BB,
            "remote",
            _inv_id_file(),
            "--runner_exec_properties=workload-isolation-type=firecracker",
            f"--container_image=docker://{IMAGE}",
            "--output_base=/tmp/bazel",
            "build",
            "--config=rbe",
            "//foo",
        ]

    def test_no_verb(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """When no verb is found, everything is treated as startup options."""
        cmd = self._build(tmp_path, ["--flag", "//foo"], monkeypatch)
        assert cmd == [
            BB,
            "remote",
            _inv_id_file(),
            "--runner_exec_properties=workload-isolation-type=firecracker",
            f"--container_image=docker://{IMAGE}",
            "--flag",
            "//foo",
            "--config=rbe",
        ]

    def test_no_repo_config_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """When bbr.json is missing, defaults apply (no runner props, no container, no bazel_args)."""
        repo = _make_repo(tmp_path)
        # No bbr.json written
        monkeypatch.setattr("devinfra.bbr._find_bb", lambda: BB)
        monkeypatch.delenv("BBR_REMOTE_ARGS", raising=False)
        monkeypatch.delenv("BBR_BAZELRC", raising=False)
        cmd = build_command(repo, ["test", "//foo"])
        assert cmd == [BB, "remote", _inv_id_file(), "test", "//foo"]


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
    devinfra/docs/bb_remote_internals.md) — it never fetches, only reports (main() turns a report into a hard error).
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
