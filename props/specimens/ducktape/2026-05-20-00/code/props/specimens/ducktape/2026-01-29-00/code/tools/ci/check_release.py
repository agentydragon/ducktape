#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["pydantic>=2.0", "pygit2>=1.14"]
# ///
"""Check if a release is needed for a specific package.

Compares against the floating latest release tag using bazel-diff to determine
if the package's wheel target has been affected.

Usage:
    PACKAGE_PREFIX=ducktape BAZEL_WHEEL_TARGET="//:wheel" \
        LATEST_RELEASE_TAG=ducktape-latest \
        uv run tools/ci/check_release.py
"""

import sys
from pathlib import Path

# Add repo root to path for tools.ci imports when running via uv
_REPO_ROOT = Path(__file__).parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from tools.ci.check_release_lib import main  # noqa: E402

if __name__ == "__main__":
    main()
