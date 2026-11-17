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
    for index, command in enumerate(json.loads(os.environ["K3S_RENDERERS"])):
        renderer = runfiles.root() / command[0]
        outputs = []
        for attempt in range(2):
            output = tmp_path / f"{index}-{renderer.name}-{attempt}.json"
            with (undeclared_outputs_dir() / f"{index}-{renderer.name}-{attempt}.log").open("wb") as log:
                subprocess.run(
                    [renderer, *command[1:]],
                    env=os.environ | {"K3S_FIXTURE_OUTPUT": str(output)},
                    check=True,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                )
            outputs.append(output.read_bytes())
        assert outputs[0] == outputs[1], command


if __name__ == "__main__":
    pytest_bazel.main()
