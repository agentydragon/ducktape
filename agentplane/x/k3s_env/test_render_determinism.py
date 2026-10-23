"""Independent chart executions must produce the same cached test inputs."""

import os
import subprocess
from pathlib import Path

import pytest_bazel

from util.bazel.runfiles import get_required_path
from util.testing.undeclared_outputs import undeclared_outputs_dir

_BASE_VARIANTS = ("none", "local-model", "public-litellm", "local-model-vm")


def _render(renderer: Path, tmp_path: Path, name: str, *, variant: str | None) -> dict[str, bytes]:
    output = tmp_path / name
    output.mkdir()
    env = os.environ.copy()
    env["K3S_FIXTURE_DIR"] = str(output)
    if variant is None:
        env.pop("K3S_FIXTURE_VARIANT", None)
    else:
        env["K3S_FIXTURE_VARIANT"] = variant
    with (undeclared_outputs_dir() / f"{name}.log").open("wb") as log:
        subprocess.run([renderer], env=env, check=True, stdout=log, stderr=subprocess.STDOUT)
    return {path.name: path.read_bytes() for path in output.glob("*.json")}


def test_independent_renders_are_byte_identical(tmp_path: Path) -> None:
    renderer = get_required_path(os.environ["K3S_RENDERER_RLOCATION"])
    for variant in _BASE_VARIANTS:
        first = _render(renderer, tmp_path, f"{variant}-0", variant=variant)
        second = _render(renderer, tmp_path, f"{variant}-1", variant=variant)
        assert set(first) == {f"{variant}.json"}
        assert first == second

    mcp_renderer = get_required_path(os.environ["K3S_MCP_RENDERER_RLOCATION"])
    first = _render(mcp_renderer, tmp_path, "mcp-0", variant=None)
    second = _render(mcp_renderer, tmp_path, "mcp-1", variant=None)
    assert set(first) == {"local-model-mcp-fixtures.json"}
    assert first == second


if __name__ == "__main__":
    pytest_bazel.main()
