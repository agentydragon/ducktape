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

from tools.ci.generate_ci_lib import main

if __name__ == "__main__":
    main()
