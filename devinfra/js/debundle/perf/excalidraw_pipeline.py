"""Run the public Excalidraw project/analyze/apply workload once."""

from __future__ import annotations

import os
import shutil
import subprocess
from enum import StrEnum
from pathlib import Path

from python.runfiles import Runfiles

ADDRESS_SPACE_BYTES, FILE_SIZE_BYTES, PHASE_TIMEOUT_SECONDS = 16 * 1024**3, 512 * 1024**2, 30 * 60


class ProfileMode(StrEnum):
    NONE = "none"
    CPU = "cpu"
    HEAP = "heap"


def runfile(runfiles: Runfiles, variable: str) -> Path:
    path = runfiles.Rlocation(os.environ[variable])
    if path is None:
        raise FileNotFoundError(f"could not resolve runfile in {variable}")
    return Path(str(path)).resolve(strict=True)


def runner_tool(name: str) -> str:
    path = shutil.which(name)
    if path is None:
        raise FileNotFoundError(f"required runner tool is not on PATH: {name}")
    return path


def input_paths(runfiles: Runfiles) -> dict[str, Path]:
    """Read declared inputs in place; config input paths are workspace-relative."""
    marker = runfile(runfiles, "DEBUNDLE_BENCH_SPEC_MARKER")
    return {
        "source_root": Path(os.environ["TEST_SRCDIR"]) / os.environ["TEST_WORKSPACE"],
        "config": runfile(runfiles, "DEBUNDLE_BENCH_CONFIG"),
        "modules": marker.parent / "modules",
        "vendor_marks": marker,
        "clsx": runfile(runfiles, "DEBUNDLE_BENCH_CLSX"),
        "roughjs": runfile(runfiles, "DEBUNDLE_BENCH_ROUGHJS"),
    }


def bounded_command(argv: list[str], timeout_seconds: int) -> list[str]:
    return [
        "timeout",
        "--signal=TERM",
        "--kill-after=15s",
        f"{timeout_seconds}s",
        "prlimit",
        f"--as={ADDRESS_SPACE_BYTES}",
        f"--fsize={FILE_SIZE_BYTES}",
        "--",
        *argv,
    ]


def phase_command(argv: list[str], *, phase: str, profile: ProfileMode, outputs: Path) -> list[str]:
    if phase != "analyze" or profile == ProfileMode.NONE:
        return argv
    if profile == ProfileMode.CPU:
        return [
            runner_tool("perf"),
            "record",
            "--event=cpu-clock:u",
            "--freq=49",
            "--call-graph=fp",
            "--compression-level=1",
            f"--output={outputs / 'perf.data'}",
            "--",
            *argv,
        ]
    return [runner_tool("heaptrack"), "--raw", "--output", str(outputs / "heaptrack"), *argv]


def execute_phase(
    phase: str,
    argv: list[str],
    *,
    profile: ProfileMode,
    outputs: Path,
    env: dict[str, str],
    timeout_seconds: int = PHASE_TIMEOUT_SECONDS,
) -> None:
    outputs.mkdir(parents=True, exist_ok=True)
    time_binary = runner_tool("time")
    child = phase_command(argv, phase=phase, profile=profile, outputs=outputs)
    command = bounded_command(
        [time_binary, "--verbose", "--output", str(outputs / "gnu-time.txt"), *child], timeout_seconds
    )
    with (outputs / "stdout.txt").open("wb") as stdout, (outputs / "stderr.txt").open("wb") as stderr:
        subprocess.run(command, env=env, stdout=stdout, stderr=stderr, check=True)


def main() -> None:
    profile = ProfileMode(os.environ.get("DEBUNDLE_BENCH_PROFILE", "none"))
    temp_root = Path(os.environ["TEST_TMPDIR"]) / "debundle-performance"
    output_root = Path(os.environ["TEST_UNDECLARED_OUTPUTS_DIR"])
    temp_root.mkdir(parents=True, exist_ok=True)
    output_root.mkdir(parents=True, exist_ok=True)
    runfiles = Runfiles.CreateOrRaise()
    inputs = input_paths(runfiles)

    binaries = {
        "projector": runfile(runfiles, "DEBUNDLE_BENCH_CANDIDATE_PROJECTOR"),
        "analyzer": runfile(runfiles, "DEBUNDLE_BENCH_CANDIDATE_ANALYZER"),
        "debundler": runfile(runfiles, "DEBUNDLE_BENCH_CANDIDATE_DEBUNDLER"),
    }
    if profile != ProfileMode.NONE:
        analyze_outputs = output_root / "analyze"
        analyze_outputs.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(binaries["analyzer"], analyze_outputs / "debundle_analyze")
    work = temp_root / "work"
    work.mkdir(parents=True, exist_ok=True)
    projection = work / "analysis.projection"
    apply_projection = work / "apply.projection"
    analysis = work / "source.analysis"

    def opts(*pairs: tuple[str, Path | str]) -> list[str]:
        return [item for flag, value in pairs for item in (f"--{flag}", str(value))]

    common = opts(
        ("tree-config", inputs["config"]),
        ("tree-modules", inputs["modules"]),
        ("tree-vendor-marks", inputs["vendor_marks"]),
        ("tree-source-root", inputs["source_root"]),
        ("package-root", f"clsx={inputs['clsx']}"),
        ("package-root", f"roughjs={inputs['roughjs']}"),
    )
    commands = {
        "project": [
            str(binaries["projector"]),
            *common,
            *opts(
                ("out-root", work / "projection-out"),
                ("out", projection),
                ("apply-out", apply_projection),
                ("projection-root", inputs["source_root"]),
            ),
        ],
        "analyze": [
            str(binaries["analyzer"]),
            *opts(
                ("projection", projection),
                ("tree-source-root", inputs["source_root"]),
                ("package-root", f"clsx={inputs['clsx']}"),
                ("package-root", f"roughjs={inputs['roughjs']}"),
                ("out", analysis),
            ),
        ],
        "apply": [
            str(binaries["debundler"]),
            "run",
            *common,
            *opts(
                ("out-root", work / "apply-out"),
                ("analysis", analysis),
                ("apply-projection", apply_projection),
                ("projection-root", inputs["source_root"]),
            ),
        ],
    }
    env = os.environ.copy()
    env.pop("DEBUNDLE_OUT", None)
    env.pop("DEBUNDLE_TREE_SOURCE_ROOT", None)
    for phase, argv in commands.items():
        execute_phase(phase, argv, profile=profile, outputs=output_root / phase, env=env)


if __name__ == "__main__":
    main()
