"""Smoke-test the released wheel without importing modules from the source tree."""

from __future__ import annotations

import configparser
import os
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest_bazel
from python.runfiles import Runfiles

# gazelle:include_dep //devinfra/pr_visuals:publisher
EXPECTED_ENTRY_POINTS = {
    "pr-visuals-announce": "devinfra.pr_visuals.check_run:main",
    "pr-visuals-publish": "devinfra.pr_visuals.publisher:cli",
}


def test_wheel_contains_import_closure_templates_and_runnable_entry_points(runfiles: Runfiles, tmp_path: Path) -> None:
    root = runfiles.root()
    with zipfile.ZipFile(root / os.environ["PR_VISUALS_WHEEL_RLOCATION"]) as wheel:
        files = set(wheel.namelist())
        assert "devinfra/pr_visuals/issue_provenance.j2" in files
        assert "devinfra/pr_visuals/determinism.py" not in files
        assert {
            "devinfra/testing/bazel_snapshot_extension.py",
            "devinfra/testing/output_writer.py",
            "devinfra/testing/pytest_outputs.py",
            "devinfra/testing/sharding.py",
            "devinfra/testing/undeclared_outputs.py",
        }.isdisjoint(files)
        assert not any(name.startswith("devinfra/testing/test_") for name in files)

        entry_points_files = [name for name in files if name.endswith(".dist-info/entry_points.txt")]
        assert len(entry_points_files) == 1
        entry_points = configparser.ConfigParser()
        entry_points.read_string(wheel.read(entry_points_files[0]).decode())
        assert dict(entry_points["console_scripts"]) == EXPECTED_ENTRY_POINTS

        install_root = tmp_path / "installed"
        wheel.extractall(install_root)

    bin_dir = install_root / "bin"
    bin_dir.mkdir()
    for name, target in EXPECTED_ENTRY_POINTS.items():
        module, function = target.split(":", 1)
        script = bin_dir / name
        script.write_text(
            "#!/usr/bin/env python3\n"
            "import importlib\n"
            "import sys\n"
            f"sys.exit(getattr(importlib.import_module({module!r}), {function!r})())\n"
        )
        script.chmod(0o755)

    # Keep only third-party Bazel runfiles on PYTHONPATH. In particular, the
    # workspace source root is absent, so imports must resolve from the wheel.
    external_python_paths = [
        str(Path(path).resolve()) for path in sys.path if path and Path(path).name == "site-packages"
    ]
    assert external_python_paths
    child_env = {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONNOUSERSITE": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPATH": os.pathsep.join([str(install_root), *external_python_paths]),
    }

    for name in EXPECTED_ENTRY_POINTS:
        result = subprocess.run(
            [sys.executable, "-S", "-P", str(bin_dir / name), "--help"],
            cwd=tmp_path,
            env=child_env,
            check=True,
            capture_output=True,
            text=True,
        )
        assert "usage:" in result.stdout.lower()

    subprocess.run(
        [
            sys.executable,
            "-S",
            "-P",
            root / "ducktape/devinfra/pr_visuals/wheel_render_probe.py",
            install_root,
            tmp_path / "fixture",
            tmp_path / "site",
        ],
        cwd=tmp_path,
        env=child_env,
        check=True,
        capture_output=True,
        text=True,
    )


if __name__ == "__main__":
    pytest_bazel.main()
