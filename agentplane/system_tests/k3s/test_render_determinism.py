"""Independent chart executions must produce the same cached test inputs."""

import os
import subprocess
from pathlib import Path

import pytest_bazel

from util.bazel.runfiles import get_required_path
from util.testing.undeclared_outputs import undeclared_outputs_dir

_EXPECTED_OUTPUTS = {
    "central-sampler.json",
    "central-sampler-vm.json",
    "local-model.json",
    "local-model-mcp-fixtures.json",
    "local-model-vm.json",
    "public-litellm.json",
}


def test_independent_renders_are_byte_identical(tmp_path: Path) -> None:
    renderer = get_required_path(os.environ["K3S_RENDERER_RLOCATION"])
    outputs = []
    for attempt in range(2):
        output = tmp_path / str(attempt)
        output.mkdir()
        with (undeclared_outputs_dir() / f"render-{attempt}.log").open("wb") as log:
            subprocess.run(
                [renderer],
                env=os.environ | {"K3S_FIXTURE_DIR": str(output)},
                check=True,
                stdout=log,
                stderr=subprocess.STDOUT,
            )
        outputs.append({path.name: path.read_bytes() for path in output.glob("*.json")})
    assert set(outputs[0]) == _EXPECTED_OUTPUTS
    assert outputs[0] == outputs[1]


if __name__ == "__main__":
    pytest_bazel.main()
