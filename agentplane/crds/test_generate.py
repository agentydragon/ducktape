"""The committed CRDs are what `generate` writes (STYLE.md § Testing, generated-output snapshot):
regenerate with `bb run //agentplane/crds:generate_bin` and commit the result when this fails.
"""

import pytest_bazel
from python.runfiles import Runfiles

from agentplane.crds.generate import render_crds
from devinfra.testing.undeclared_outputs import undeclared_outputs_dir


def test_committed_files_are_generated(runfiles: Runfiles) -> None:
    for relative, content in render_crds(runfiles.root()):
        (undeclared_outputs_dir() / relative.name).write_text(content)
        assert content == (runfiles.root() / "ducktape" / relative).read_text(), f"{relative} is stale"


if __name__ == "__main__":
    pytest_bazel.main()
