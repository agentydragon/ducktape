from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest
import pytest_bazel

from devinfra.ci.pr_mergeability import SourceRevisionError
from devinfra.ci.precommit_file_selection import main, needs_ansible_roles, prepare_changed_files


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


@dataclass(frozen=True)
class PullRequestCheckout:
    origin: Path
    path: Path
    merge_sha: str
    head_sha: str


@pytest.fixture
def checkout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> PullRequestCheckout:
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    origin = tmp_path / "origin"
    origin.mkdir()
    _git(origin, "init", "--initial-branch=devel")
    _git(origin, "config", "user.name", "Test Author")
    _git(origin, "config", "user.email", "test@example.invalid")
    for name in ("upstream.txt", "removed.txt", "old-name.txt"):
        (origin / name).write_text(name + "\n")
    _git(origin, "add", ".")
    _git(origin, "commit", "-m", "base")
    _git(origin, "switch", "-c", "pr")
    for name in ("pr.txt", "space and\n雪.txt", ".ansible-lint.yaml"):
        (origin / name).write_text("PR change\n")
    (origin / "removed.txt").unlink()
    _git(origin, "mv", "old-name.txt", "new-name.txt")
    _git(origin, "add", "--all")
    _git(origin, "commit", "-m", "PR")
    head_sha = _git(origin, "rev-parse", "HEAD")
    _git(origin, "switch", "-c", "tested-merge", "devel")
    _git(origin, "merge", "--no-ff", "pr", "-m", "test merge")
    merge_sha = _git(origin, "rev-parse", "HEAD")
    path = tmp_path / "checkout"
    _git(origin, "clone", "--depth=2", "--no-single-branch", "--branch=tested-merge", origin.as_uri(), str(path))
    _git(path, "config", "user.name", "Test Author")
    _git(path, "config", "user.email", "test@example.invalid")
    monkeypatch.chdir(path)
    return PullRequestCheckout(origin=origin, path=path, merge_sha=merge_sha, head_sha=head_sha)


def test_base_advancing_after_checkout_does_not_select_upstream_edits(
    checkout: PullRequestCheckout, tmp_path: Path
) -> None:
    _git(checkout.origin, "switch", "devel")
    (checkout.origin / "upstream.txt").write_text("landed after the test merge\n")
    _git(checkout.origin, "commit", "-am", "advance base")
    _git(checkout.path, "fetch", "--depth=1", "origin", "devel")
    changed_files = tmp_path / "changed-files"

    needs_roles, paths = prepare_changed_files(
        event_name="pull_request",
        expected_sha=checkout.merge_sha,
        expected_head_sha=checkout.head_sha,
        changed_files_path=changed_files,
    )

    assert paths == [".ansible-lint.yaml", "new-name.txt", "pr.txt", "space and\n雪.txt"]
    assert changed_files.read_bytes() == ".ansible-lint.yaml\0new-name.txt\0pr.txt\0space and\n雪.txt\0".encode()
    assert needs_roles


@pytest.mark.parametrize("path", ["README.md", "agentplane/app/frontend/src/App.tsx", "docs/ci.md"])
def test_unrelated_pr_files_skip_ansible_roles(path: str) -> None:
    assert not needs_ansible_roles("pull_request", [path])


@pytest.mark.parametrize(
    "path",
    [
        "ansible/atlas.yaml",
        "ansible/roles/gui/tasks/main.yml",
        ".ansible-lint.yaml",
        ".github/scripts/run-ansible-lint.sh",
        ".github/workflows/ansible-lint.yml",
        ".github/workflows/pre-commit.yml",
        ".pre-commit-config.yaml",
    ],
)
def test_ansible_files_and_configuration_keep_ansible_roles(path: str) -> None:
    assert needs_ansible_roles("pull_request", [path])


@pytest.mark.parametrize("event_name", ["push", "workflow_dispatch"])
def test_non_pr_events_keep_all_file_role_setup(event_name: str) -> None:
    assert needs_ansible_roles(event_name, [])


@pytest.mark.parametrize("event_name", ["push", "workflow_dispatch"])
def test_non_pr_selection_keeps_roles_without_running_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, event_name: str
) -> None:
    monkeypatch.chdir(tmp_path)
    changed_files = tmp_path / "changed-files"
    needs_roles, paths = prepare_changed_files(
        event_name=event_name, expected_sha="", expected_head_sha="", changed_files_path=changed_files
    )
    assert needs_roles
    assert paths is None
    assert changed_files.read_bytes() == b""


def test_unknown_selection_keeps_ansible_roles() -> None:
    assert needs_ansible_roles("pull_request", None)


def test_empty_pr_diff_is_known_and_has_no_ansible_roles(checkout: PullRequestCheckout, tmp_path: Path) -> None:
    _git(checkout.path, "switch", "-c", "empty-pr")
    _git(checkout.path, "commit", "--allow-empty", "-m", "empty PR")
    head_sha = _git(checkout.path, "rev-parse", "HEAD")
    _git(checkout.path, "switch", "--detach", checkout.merge_sha)
    _git(checkout.path, "merge", "--no-ff", "empty-pr", "-m", "empty test merge")
    merge_sha = _git(checkout.path, "rev-parse", "HEAD")
    changed_files = tmp_path / "changed-files"

    needs_roles, paths = prepare_changed_files(
        event_name="pull_request", expected_sha=merge_sha, expected_head_sha=head_sha, changed_files_path=changed_files
    )

    assert not needs_roles
    assert paths == []
    assert changed_files.read_bytes() == b""


@pytest.mark.parametrize("mismatch", ["checkout", "head", "non_merge", "missing_metadata"])
def test_invalid_pr_source_does_not_publish_a_selection(
    checkout: PullRequestCheckout, tmp_path: Path, mismatch: str
) -> None:
    expected_sha = checkout.merge_sha
    expected_head_sha = checkout.head_sha
    if mismatch == "checkout":
        expected_sha = checkout.head_sha
    elif mismatch == "head":
        expected_head_sha = checkout.merge_sha
    elif mismatch == "non_merge":
        _git(checkout.path, "switch", "--detach", checkout.head_sha)
        expected_sha = checkout.head_sha
    else:
        expected_head_sha = ""
    changed_files = tmp_path / "changed-files"

    with pytest.raises(SourceRevisionError):
        prepare_changed_files(
            event_name="pull_request",
            expected_sha=expected_sha,
            expected_head_sha=expected_head_sha,
            changed_files_path=changed_files,
        )

    assert not changed_files.exists()


def test_depth_one_checkout_does_not_publish_a_selection(
    checkout: PullRequestCheckout, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shallow = tmp_path / "shallow"
    _git(checkout.origin, "clone", "--depth=1", "--branch=tested-merge", checkout.origin.as_uri(), str(shallow))
    monkeypatch.chdir(shallow)
    changed_files = tmp_path / "changed-files"

    with pytest.raises(SourceRevisionError):
        prepare_changed_files(
            event_name="pull_request",
            expected_sha=checkout.merge_sha,
            expected_head_sha=checkout.head_sha,
            changed_files_path=changed_files,
        )

    assert not changed_files.exists()


def test_git_failure_does_not_become_an_empty_selection(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    changed_files = tmp_path / "changed-files"

    with pytest.raises(subprocess.CalledProcessError):
        prepare_changed_files(
            event_name="pull_request",
            expected_sha="missing",
            expected_head_sha="missing",
            changed_files_path=changed_files,
        )

    assert not changed_files.exists()


def test_workflow_entrypoint_publishes_verified_selection(
    checkout: PullRequestCheckout, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request")
    monkeypatch.setenv("GITHUB_SHA", checkout.merge_sha)
    monkeypatch.setenv("EXPECTED_HEAD_SHA", checkout.head_sha)
    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path))
    output = tmp_path / "github-output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))

    main()

    assert output.read_text() == "needs_ansible_roles=true\n"
    assert (tmp_path / "pre-commit-changed-files").read_bytes() == (
        ".ansible-lint.yaml\0new-name.txt\0pr.txt\0space and\n雪.txt\0".encode()
    )


if __name__ == "__main__":
    pytest_bazel.main()
