from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest
import pytest_bazel

from devinfra.ci import precommit_file_selection
from devinfra.ci.precommit_file_selection import needs_ansible_roles, needs_bazel_setup, prepare_changed_files


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


def test_bazel_setup_skips_only_when_all_changed_python_files_are_resolved(tmp_path: Path) -> None:
    (tmp_path / "test_bazel_main.py").write_text("pytest_bazel.main()\n")
    (tmp_path / "test_pytest_main.py").write_text("pytest.main(['tests'])\n")

    assert not needs_bazel_setup("pull_request", ["README.md", "test_bazel_main.py", "test_pytest_main.py"], tmp_path)
    assert not needs_bazel_setup("pull_request", ["README.md"], tmp_path)


@pytest.mark.parametrize(
    ("event_name", "changed_paths", "enforce_bazel_tests"),
    [
        ("push", ["README.md"], None),
        ("workflow_dispatch", ["README.md"], None),
        ("schedule", ["README.md"], None),
        ("pull_request", None, None),
        ("pull_request", ["test_unmarked.py"], None),
        ("pull_request", ["test_bazel_main.py", "test_unmarked.py"], None),
        ("pull_request", ["README.md"], "1"),
        ("pull_request", ["README.md"], "true"),
    ],
)
def test_bazel_setup_is_retained_for_unresolved_or_full_selections(
    tmp_path: Path, event_name: str, changed_paths: list[str] | None, enforce_bazel_tests: str | None
) -> None:
    (tmp_path / "test_bazel_main.py").write_text("pytest_bazel.main()\n")
    (tmp_path / "test_unmarked.py").write_text("def test_example(): pass\n")

    assert needs_bazel_setup(event_name, changed_paths, tmp_path, enforce_bazel_tests)


def test_bazel_setup_is_retained_when_python_file_cannot_be_read(tmp_path: Path) -> None:
    assert needs_bazel_setup("pull_request", ["deleted_test.py"], tmp_path)


def _run_preflight_main(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, run: Callable[..., subprocess.CompletedProcess[bytes]]
) -> Path:
    runner_temp = tmp_path / "runner-temp"
    runner_temp.mkdir()
    github_output = tmp_path / "github-output"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request")
    monkeypatch.setenv("GITHUB_BASE_REF", "devel")
    monkeypatch.setenv("RUNNER_TEMP", str(runner_temp))
    monkeypatch.setenv("GITHUB_OUTPUT", str(github_output))
    monkeypatch.delenv("DUCKTAPE_PRECOMMIT_ENFORCE_BAZEL_TESTS", raising=False)
    real_prepare_changed_files = prepare_changed_files

    def prepare_with_fake_git(
        *, event_name: str, base_ref: str, changed_files_path: Path
    ) -> tuple[bool, list[str] | None]:
        return real_prepare_changed_files(
            event_name=event_name, base_ref=base_ref, changed_files_path=changed_files_path, run=run
        )

    monkeypatch.setattr(precommit_file_selection, "prepare_changed_files", prepare_with_fake_git)
    precommit_file_selection.main()
    return github_output


@pytest.mark.parametrize(
    ("changed_paths", "file_contents", "needs_bazel"),
    [
        (["README.md", "test_marked.py"], {"test_marked.py": "pytest_bazel.main()\n"}, False),
        (
            ["props/specimens/example/snapshot/code/test_marked.py"],
            {"props/specimens/example/snapshot/code/test_marked.py": "pytest_bazel.main()\n"},
            False,
        ),
        (["test_unmarked.py"], {"test_unmarked.py": "def test_example(): pass\n"}, True),
    ],
)
def test_preflight_outputs_bazel_gate_from_pr_diff(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    changed_paths: list[str],
    file_contents: dict[str, str],
    needs_bazel: bool,
) -> None:
    for path, content in file_contents.items():
        file_path = tmp_path / path
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content)
    diff_output = b"".join(f"{path}\0".encode() for path in changed_paths)
    calls = iter(
        [
            subprocess.CompletedProcess(["git", "fetch"], 0),
            subprocess.CompletedProcess(["git", "diff"], 0, stdout=diff_output),
        ]
    )
    github_output = _run_preflight_main(tmp_path, monkeypatch, lambda *_args, **_kwargs: next(calls))

    assert github_output.read_text() == (
        f"needs_ansible_roles=false\nneeds_bazel_setup={'true' if needs_bazel else 'false'}\n"
    )
    assert (tmp_path / "runner-temp" / "pre-commit-changed-files").read_bytes() == diff_output


def test_preflight_does_not_publish_a_skip_after_diff_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    run_calls = 0

    def fail_diff(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        nonlocal run_calls
        run_calls += 1
        if run_calls == 1:
            return subprocess.CompletedProcess(["git", "fetch"], 0)
        raise subprocess.CalledProcessError(1, ["git", "diff"])

    with pytest.raises(subprocess.CalledProcessError):
        _run_preflight_main(tmp_path, monkeypatch, fail_diff)

    assert not (tmp_path / "github-output").exists()


if __name__ == "__main__":
    pytest_bazel.main()
