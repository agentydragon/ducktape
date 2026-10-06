from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import pytest_bazel

from devinfra.ci.precommit_file_selection import needs_ansible_roles, prepare_changed_files


@pytest.mark.parametrize("path", ["README.md", "agentplane/app/frontend/src/App.tsx", "docs/ci.md"])
def test_unrelated_pr_files_skip_ansible_roles(path: str) -> None:
    assert not needs_ansible_roles("pull_request", [path])


@pytest.mark.parametrize(
    "path",
    [
        "ansible/devel.yaml",
        "ansible/roles/common/tasks/main.yml",
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


def test_non_pr_selection_keeps_roles_without_running_git(tmp_path: Path) -> None:
    changed_files = tmp_path / "changed-files"

    needs_roles, paths = prepare_changed_files(
        event_name="push",
        base_ref="",
        changed_files_path=changed_files,
        run=lambda *_args, **_kwargs: pytest.fail("non-PR events do not diff files"),
    )

    assert needs_roles
    assert paths is None
    assert changed_files.read_bytes() == b""


def test_unknown_selection_keeps_ansible_roles() -> None:
    assert needs_ansible_roles("pull_request", None)


def test_empty_pr_diff_is_known_and_has_no_ansible_roles(tmp_path: Path) -> None:
    changed_files = tmp_path / "changed-files"
    calls = iter(
        [subprocess.CompletedProcess(["git", "fetch"], 0), subprocess.CompletedProcess(["git", "diff"], 0, stdout=b"")]
    )

    needs_roles, paths = prepare_changed_files(
        event_name="pull_request",
        base_ref="devel",
        changed_files_path=changed_files,
        run=lambda *_args, **_kwargs: next(calls),
    )

    assert not needs_roles
    assert paths == []
    assert changed_files.read_bytes() == b""


def test_changed_paths_are_nul_delimited_and_keep_roles_for_ansible(tmp_path: Path) -> None:
    changed_files = tmp_path / "changed-files"
    calls = iter(
        [
            subprocess.CompletedProcess(["git", "fetch"], 0),
            subprocess.CompletedProcess(["git", "diff"], 0, stdout=b"README.md\0ansible/devel.yaml\0"),
        ]
    )

    needs_roles, paths = prepare_changed_files(
        event_name="pull_request",
        base_ref="devel",
        changed_files_path=changed_files,
        run=lambda *_args, **_kwargs: next(calls),
    )

    assert needs_roles
    assert paths == ["README.md", "ansible/devel.yaml"]
    assert changed_files.read_bytes() == b"README.md\0ansible/devel.yaml\0"


def test_diff_failure_does_not_become_an_empty_selection(tmp_path: Path) -> None:
    changed_files = tmp_path / "changed-files"
    calls = 0

    def run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        nonlocal calls
        calls += 1
        if calls == 1:
            return subprocess.CompletedProcess(["git", "fetch"], 0)
        raise subprocess.CalledProcessError(1, ["git", "diff"])

    with pytest.raises(subprocess.CalledProcessError):
        prepare_changed_files(event_name="pull_request", base_ref="devel", changed_files_path=changed_files, run=run)

    assert not changed_files.exists()


if __name__ == "__main__":
    pytest_bazel.main()
