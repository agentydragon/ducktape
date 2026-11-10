import os
import zipfile
from pathlib import Path

import pytest_bazel
from python.runfiles import Runfiles


def _runfile(runfiles: Runfiles, env_var: str) -> Path:
    location = runfiles.Rlocation(os.environ[env_var])
    assert location is not None
    return Path(location)


def test_both_skill_archives_include_the_canonical_reporting_reference() -> None:
    runfiles = Runfiles.CreateOrRaise()
    source = _runfile(runfiles, "REPORTING_SOURCE").read_bytes()

    for skill_name, env_var in (("ci_latency", "CI_LATENCY_ARCHIVE"), ("ci_reliability", "CI_RELIABILITY_ARCHIVE")):
        archive_path = _runfile(runfiles, env_var)
        with zipfile.ZipFile(archive_path) as archive:
            packaged = archive.read(f"{skill_name}/references/reporting.md")
        assert packaged == source, f"{skill_name} ships a stale shared reporting reference"


if __name__ == "__main__":
    pytest_bazel.main()
