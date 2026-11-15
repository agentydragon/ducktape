"""Independent chart executions must produce the same cached test inputs."""

import json
import os
import subprocess
from pathlib import Path

import pytest_bazel
from python.runfiles import Runfiles

from devinfra.testing.undeclared_outputs import undeclared_outputs_dir


def test_independent_renders_are_byte_identical(tmp_path: Path) -> None:
    runfiles = Runfiles.CreateOrRaise()
    for location in json.loads(os.environ["K3S_RENDERERS"]):
        renderer = runfiles.root() / location
        outputs = []
        for attempt in range(2):
            output = tmp_path / f"{renderer.name}-{attempt}.json"
            with (undeclared_outputs_dir() / f"{renderer.name}-{attempt}.log").open("wb") as log:
                subprocess.run(
                    [renderer],
                    env=os.environ | {"K3S_FIXTURE_OUTPUT": str(output)},
                    check=True,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                )
            outputs.append(output.read_bytes())
        assert outputs[0] == outputs[1], location


if __name__ == "__main__":
    pytest_bazel.main()
