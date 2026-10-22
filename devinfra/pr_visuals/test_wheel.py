"""Smoke-test the released wheel without importing modules from the source tree."""

from __future__ import annotations

import configparser
import os
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest_bazel

from util.bazel.runfiles import get_required_path, own_repo_rlocation

# gazelle:include_dep //devinfra/pr_visuals:publisher
WHEEL = get_required_path(own_repo_rlocation("devinfra/pr_visuals/pr_visuals-0.1.0-py3-none-any.whl"))
EXPECTED_ENTRY_POINTS = {
    "pr-visuals-announce": "devinfra.pr_visuals.check_run:main",
    "pr-visuals-publish": "devinfra.pr_visuals.publisher:main",
}


def test_wheel_contains_import_closure_templates_and_runnable_entry_points(tmp_path: Path) -> None:
    with zipfile.ZipFile(WHEEL) as wheel:
        files = set(wheel.namelist())
        assert {
            "devinfra/pr_visuals/check_run.py",
            "devinfra/pr_visuals/publisher.py",
            "devinfra/pr_visuals/gallery.css",
            "devinfra/pr_visuals/gallery_sort.js",
            "devinfra/pr_visuals/gallery_slider.css",
            "devinfra/pr_visuals/gallery_slider.js",
            "devinfra/pr_visuals/pr_visual_test.html.j2",
            "devinfra/pr_visuals/pr_visuals.html.j2",
            "devinfra/ci/invocation_ids.py",
            "util/visual_diff.py",
            "util/visual_review.py",
        } <= files
        assert "devinfra/pr_visuals/determinism.py" not in files

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
            str(get_required_path("_main/devinfra/pr_visuals/wheel_render_probe.py")),
            str(install_root),
            str(tmp_path / "fixture"),
            str(tmp_path / "site"),
        ],
        cwd=tmp_path,
        env=child_env,
        check=True,
        capture_output=True,
        text=True,
    )


if __name__ == "__main__":
    pytest_bazel.main()
