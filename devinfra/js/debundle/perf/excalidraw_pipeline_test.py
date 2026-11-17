"""Focused subprocess checks for native benchmark execution."""

import os
import subprocess
import sys
from pathlib import Path

import pytest
import pytest_bazel
import yaml
from python.runfiles import Runfiles

from devinfra.js.debundle.perf import excalidraw_pipeline


def test_excalidraw_runfiles_are_readable_without_copying() -> None:
    inputs = excalidraw_pipeline.input_paths(Runfiles.CreateOrRaise())
    config = yaml.safe_load(inputs["config"].read_text())["inputs"]
    source = inputs["source_root"] / config["root"]
    listing = inputs["source_root"] / config["js_list_path"]
    for line in listing.read_text().splitlines():
        assert (source / line.split("\t")[0]).is_file()
    assert list(inputs["modules"].rglob("*.yaml"))
    assert inputs["vendor_marks"].is_file()
    for package, subpath in {"clsx": "dist/clsx.m.js", "roughjs": "bin/rough.js"}.items():
        root = inputs[package]
        assert (root / "package.json").is_file()
        # Match Debundle's realpath containment requirement for vendor files.
        assert (root / subpath).resolve(strict=True).is_relative_to(root)


def test_execute_phase_captures_child_output_and_gnu_time(tmp_path: Path) -> None:
    output = tmp_path / "outputs"
    excalidraw_pipeline.execute_phase(
        "project",
        [sys.executable, "-c", "import sys; print('child stdout'); print('child stderr', file=sys.stderr)"],
        profile=excalidraw_pipeline.ProfileMode.NONE,
        outputs=output,
        env=os.environ.copy(),
        timeout_seconds=10,
    )

    assert "child stdout" in (output / "stdout.txt").read_text()
    assert "child stderr" in (output / "stderr.txt").read_text()
    assert "Command being timed" in (output / "gnu-time.txt").read_text()


def test_execute_phase_preserves_logs_and_propagates_child_failure(tmp_path: Path) -> None:
    output = tmp_path / "outputs"
    with pytest.raises(subprocess.CalledProcessError) as failure:
        excalidraw_pipeline.execute_phase(
            "analyze",
            [sys.executable, "-c", "import sys; print('partial output'); raise SystemExit(23)"],
            profile=excalidraw_pipeline.ProfileMode.NONE,
            outputs=output,
            env=os.environ.copy(),
            timeout_seconds=10,
        )

    assert failure.value.returncode == 23
    assert "partial output" in (output / "stdout.txt").read_text()
    assert "Command being timed" in (output / "gnu-time.txt").read_text()


@pytest.mark.parametrize(
    ("profile", "tool", "absent"),
    [
        (excalidraw_pipeline.ProfileMode.CPU, "perf", "heaptrack"),
        (excalidraw_pipeline.ProfileMode.HEAP, "heaptrack", "perf"),
    ],
)
def test_profile_modes_are_separate(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, profile: excalidraw_pipeline.ProfileMode, tool: str, absent: str
) -> None:
    monkeypatch.setattr(excalidraw_pipeline, "runner_tool", lambda name: name)
    analyzer = ["analyzer", "--projection", "input"]
    command = excalidraw_pipeline.phase_command(analyzer, phase="analyze", profile=profile, outputs=tmp_path)
    assert command[0] == tool
    assert absent not in command
    assert command[-len(analyzer) :] == analyzer
    assert excalidraw_pipeline.phase_command(analyzer, phase="project", profile=profile, outputs=tmp_path) == analyzer


if __name__ == "__main__":
    pytest_bazel.main()
