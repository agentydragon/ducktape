"""Smoke-test the Gmail classifier wheel outside the source tree."""

from __future__ import annotations

import os
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest_bazel

# gazelle:include_dep :event_classifier
from util.bazel.runfiles import get_required_path, own_repo_rlocation

WHEEL = get_required_path(own_repo_rlocation("gmail_archiver/gmail_archiver-0.1.0-py3-none-any.whl"))


def test_wheel_contains_classifier_import_closure(tmp_path: Path) -> None:
    with zipfile.ZipFile(WHEEL) as wheel:
        files = set(wheel.namelist())
        assert "gmail_archiver/event_classifier.py" in files
        assert "openai_utils/json_schema.py" in files
        install_root = tmp_path / "installed"
        wheel.extractall(install_root)

    # The workspace source root is absent; imports must come from the wheel.
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

    subprocess.run(
        [
            sys.executable,
            "-S",
            "-P",
            "-c",
            "from gmail_archiver.event_classifier import EmailTemplateExtraction; "
            "from openai_utils.json_schema import openai_json_schema; "
            "openai_json_schema(EmailTemplateExtraction)",
        ],
        cwd=tmp_path,
        env=child_env,
        check=True,
        capture_output=True,
        text=True,
    )


if __name__ == "__main__":
    pytest_bazel.main()
