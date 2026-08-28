import os
import zipfile
from pathlib import Path

import pytest_bazel

from skills.frontmatter_validation import validate_skill_frontmatter_text


def test_frontmatter_archives() -> None:
    archive_path = os.environ.get("SKILL_ARCHIVE")
    assert archive_path, "expected SKILL_ARCHIVE env var"

    archive = Path(archive_path)
    with zipfile.ZipFile(archive) as zf:
        skill_members = [name for name in zf.namelist() if name.endswith("/SKILL.md")]

        assert skill_members, f"{archive}: expected at least one SKILL.md in packaged archive"

        for name in skill_members:
            validate_skill_frontmatter_text(zf.read(name).decode(), source=f"{archive}:{name}")


if __name__ == "__main__":
    pytest_bazel.main()
