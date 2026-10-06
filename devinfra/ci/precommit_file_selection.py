"""Select changed paths for pre-commit CI and Ansible role provisioning."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable
from pathlib import Path

Runner = Callable[..., subprocess.CompletedProcess[bytes]]

ANSIBLE_CONFIGURATION_PATHS = frozenset(
    {
        ".ansible-lint.yaml",
        ".github/scripts/run-ansible-lint.sh",
        ".github/workflows/ansible-lint.yml",
        ".github/workflows/pre-commit.yml",
        ".pre-commit-config.yaml",
    }
)


def needs_ansible_roles(event_name: str, changed_paths: list[str] | None) -> bool:
    """Keep roles for full or unknown selections; skip only proven unrelated PRs."""
    if event_name != "pull_request" or changed_paths is None:
        return True
    return any(
        path == "ansible" or path.startswith("ansible/") or path in ANSIBLE_CONFIGURATION_PATHS
        for path in changed_paths
    )


def _paths_from_git_output(output: bytes) -> list[str]:
    return [os.fsdecode(path) for path in output.split(b"\0") if path]


def prepare_changed_files(
    *, event_name: str, base_ref: str, changed_files_path: Path, run: Runner = subprocess.run
) -> tuple[bool, list[str] | None]:
    """Write NUL-delimited PR paths and return whether Ansible roles are required.

    A failed fetch or diff raises before the workflow publishes a skip decision. For
    non-PR events the hook checks all files, so role provisioning is always retained.
    """
    if event_name != "pull_request":
        changed_files_path.write_bytes(b"")
        return True, None

    if not base_ref:
        raise ValueError("GITHUB_BASE_REF is required for pull_request file selection")

    run(["git", "fetch", "--depth=1", "origin", base_ref], check=True)
    result = run(
        ["git", "diff", "--name-only", "--diff-filter=d", "-z", f"origin/{base_ref}", "HEAD"],
        check=True,
        capture_output=True,
    )
    if result.stdout is None:
        raise RuntimeError("git diff did not return changed paths")
    changed_files_path.write_bytes(result.stdout)
    changed_paths = _paths_from_git_output(result.stdout)
    return needs_ansible_roles(event_name, changed_paths), changed_paths


def main() -> None:
    event_name = os.environ["GITHUB_EVENT_NAME"]
    base_ref = os.environ.get("GITHUB_BASE_REF", "")
    changed_files_path = Path(os.environ["RUNNER_TEMP"]) / "pre-commit-changed-files"
    needs_roles, changed_paths = prepare_changed_files(
        event_name=event_name, base_ref=base_ref, changed_files_path=changed_files_path
    )

    with Path(os.environ["GITHUB_OUTPUT"]).open("a", encoding="utf-8") as output:
        output.write(f"needs_ansible_roles={'true' if needs_roles else 'false'}\n")

    if not needs_roles:
        count = len(changed_paths or [])
        print(f"Skipping Ansible Galaxy role setup for {count} unrelated changed file(s).")


if __name__ == "__main__":
    main()
