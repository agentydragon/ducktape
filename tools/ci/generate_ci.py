#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["pydantic>=2.0", "pyyaml>=6.0"]
# ///
"""Generate .github/workflows/ci.yml from workflows.yaml.

This script reads the workflow definitions and generates the CI workflow file,
eliminating duplication in job definitions.

Usage:
    uv run tools/ci/generate_ci.py
    uv run tools/ci/generate_ci.py --check  # Verify ci.yml is up to date
"""

import sys
from pathlib import Path

# Add repo root to path for tools.ci imports when running via uv
_REPO_ROOT = Path(__file__).parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from tools.ci.generate_ci_lib import main  # noqa: E402

if __name__ == "__main__":
    main()
