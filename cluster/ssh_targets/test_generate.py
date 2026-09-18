"""Generated-output snapshot (STYLE.md § Testing): the committed files are what `render` produces
from the committed sources, so ssh-mcp and sshpiper cannot pin different devbox identities."""

import pytest_bazel

from cluster.ssh_targets.generate import render
from util.bazel.runfiles import get_required_path


def test_committed_files_match_generator() -> None:
    """Regenerate with `bb run //cluster/ssh_targets:generate_bin` and commit the result if this fails."""
    for relative, text in render(lambda source: get_required_path(f"_main/{source}")).items():
        assert get_required_path(f"_main/{relative}").read_text() == text, f"{relative} is stale"


if __name__ == "__main__":
    pytest_bazel.main()
