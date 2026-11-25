"""Run the worker recipes with their packaged files and declared tools."""

import os
import shutil
from pathlib import Path

import pytest
from python.runfiles import Runfiles

from devinfra.js.debundle.skills.debundle_lane_worker.debundle_lane_worker_skill_spec import SPEC


@pytest.fixture
def shared(runfiles: Runfiles, tmp_path: Path) -> Path:
    archive = runfiles.root() / SPEC.archive_rlocation
    shutil.unpack_archive(archive, tmp_path / "skill", format="zip")
    return tmp_path / "skill" / SPEC.package_name / "references/skills/shared"


@pytest.fixture
def engine(runfiles: Runfiles) -> str:
    return str(runfiles.root() / os.environ["DEBUNDLE_ENGINE"])


@pytest.fixture
def node(runfiles: Runfiles) -> str:
    return str(runfiles.root() / os.environ["DEBUNDLE_NODE"])


@pytest.fixture
def original(runfiles: Runfiles, tmp_path: Path) -> Path:
    fixture = runfiles.root() / "ducktape/devinfra/js/debundle/skills/shared/examples/fixture"
    return shutil.copytree(fixture, tmp_path / "original inputs")
