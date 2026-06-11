#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["pydantic>=2.0", "pygit2>=1.14", "pyyaml>=6.0"]
# ///
"""CI decision engine - computes affected targets and workflows to run.

Reads workflow definitions from workflows.yaml and uses bazel-diff to compute
exactly which Bazel targets are affected. Outputs a JSON list of workflows
to trigger instead of individual boolean flags.

Requires GITHUB_OUTPUT environment variable to be set.
Requires BAZEL_DIFF_JAR environment variable pointing to bazel-diff JAR.
"""

import sys
from pathlib import Path

# Add repo root to path for tools.ci imports when running via uv
_REPO_ROOT = Path(__file__).parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from tools.ci.ci_decide_lib import main  # noqa: E402

if __name__ == "__main__":
    main()
