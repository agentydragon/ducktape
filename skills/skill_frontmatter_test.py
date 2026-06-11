import os
import tarfile
from pathlib import Path

import pytest_bazel

from skills.frontmatter_validation import validate_skill_frontmatter_text


def test_frontmatter_tarballs() -> None:
    tar_path = os.environ.get("SKILL_TAR")
    assert tar_path, "expected SKILL_TAR env var"

    tarball = Path(tar_path)
    with tarfile.open(tarball) as archive:
        skill_members = [member for member in archive.getmembers() if member.name.endswith("/SKILL.md")]

        assert skill_members, f"{tarball}: expected at least one SKILL.md in packaged tar"

        for member in skill_members:
            extracted = archive.extractfile(member)
            assert extracted is not None, f"{tarball}: could not read {member.name}"
            validate_skill_frontmatter_text(extracted.read().decode(), source=f"{tarball}:{member.name}")


if __name__ == "__main__":
    pytest_bazel.main()
