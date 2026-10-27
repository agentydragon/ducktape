"""Select changed paths for pre-commit CI and Ansible role provisioning."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from devinfra.ci.pr_mergeability import SourceRevisionError, verify_source_revision

ANSIBLE_CONFIGURATION_PATHS = {
    ".ansible-lint.yaml",
    ".github/scripts/run-ansible-lint.sh",
    ".github/workflows/ansible-lint.yml",
    ".github/workflows/pre-commit.yml",
    ".pre-commit-config.yaml",
}


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
    *, event_name: str, expected_sha: str, expected_head_sha: str, changed_files_path: Path
) -> tuple[bool, list[str] | None]:
    """Write NUL-delimited PR paths and return whether Ansible roles are required.

    A failed source verification or diff raises before the workflow publishes a skip decision. For
    non-PR events the hook checks all files, so role provisioning is always retained.
    """
    if event_name != "pull_request":
        changed_files_path.write_bytes(b"")
        return True, None

    if not expected_sha or not expected_head_sha:
        raise SourceRevisionError("PR file selection requires the tested merge SHA and expected PR head SHA")

    resolved_sha, *parents = (
        subprocess.check_output(["git", "rev-list", "--parents", "-n", "1", "HEAD"]).decode().split()
    )
    if len(parents) != 2:
        raise SourceRevisionError(f"PR checkout {resolved_sha} must expose two merge parents; use fetch-depth: 2")
    tested_sha = verify_source_revision(
        resolved_sha, expected_sha=expected_sha, expected_head_sha=expected_head_sha, second_parent_sha=parents[1]
    )
    # The live base branch may have advanced since GitHub constructed this merge.
    output = subprocess.check_output(["git", "diff", "--name-only", "--diff-filter=d", "-z", parents[0], tested_sha])
    changed_files_path.write_bytes(output)
    changed_paths = _paths_from_git_output(output)
    return needs_ansible_roles(event_name, changed_paths), changed_paths


def main() -> None:
    event_name = os.environ["GITHUB_EVENT_NAME"]
    changed_files_path = Path(os.environ["RUNNER_TEMP"]) / "pre-commit-changed-files"
    needs_roles, changed_paths = prepare_changed_files(
        event_name=event_name,
        expected_sha=os.environ["GITHUB_SHA"],
        expected_head_sha=os.environ.get("EXPECTED_HEAD_SHA", ""),
        changed_files_path=changed_files_path,
    )

    with Path(os.environ["GITHUB_OUTPUT"]).open("a", encoding="utf-8") as output:
        output.write(f"needs_ansible_roles={'true' if needs_roles else 'false'}\n")

    if not needs_roles:
        count = len(changed_paths or [])
        print(f"Skipping Ansible Galaxy role setup for {count} unrelated changed file(s).")


if __name__ == "__main__":
    main()
