import os
import zipfile
from pathlib import Path

import pytest
import pytest_bazel
from python.runfiles import Runfiles


def _runfile(runfiles: Runfiles, env_var: str) -> Path:
    location = runfiles.Rlocation(os.environ[env_var])
    assert location is not None
    return Path(location)


@pytest.mark.parametrize(
    ("reference", "source_env_var"), [("reporting.md", "REPORTING_SOURCE"), ("cdviz.md", "CDVIZ_SOURCE")]
)
def test_both_skill_archives_include_the_canonical_shared_reference(
    runfiles: Runfiles, reference: str, source_env_var: str
) -> None:
    source = _runfile(runfiles, source_env_var).read_bytes()

    for skill_name, env_var in (("ci_latency", "CI_LATENCY_ARCHIVE"), ("ci_reliability", "CI_RELIABILITY_ARCHIVE")):
        archive_path = _runfile(runfiles, env_var)
        with zipfile.ZipFile(archive_path) as archive:
            packaged = archive.read(f"{skill_name}/references/{reference}")
        assert packaged == source, f"{skill_name} ships a stale shared {reference}"


if __name__ == "__main__":
    pytest_bazel.main()
