#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["pydantic>=2.0", "pygit2>=1.14", "pyyaml>=6.0"]
# ///
"""Compute affected Bazel targets using bazel-diff.

Supports two modes:
1. CI mode (default): Compare against previous commit or merge-base for PRs
2. Release mode: Compare against last release tag for a specific package

Usage:
    # CI mode
    uv run tools/ci/bazel_diff.py

    # Release mode
    RELEASE_MODE=1 PACKAGE_PREFIX=ducktape BAZEL_TARGET_PATTERN="//..." uv run tools/ci/bazel_diff.py

Outputs to $GITHUB_OUTPUT:
    targets: space-separated list of affected targets, or "//..." for full build
    has_changes: "true" or "false"

    # CI mode only:
    has_props: "true" if //props/... targets are affected
    has_editor_agent: "true" if //editor_agent/... targets are affected
    has_agent_server: "true" if //agent_server/... targets are affected
    has_finance: "true" if //finance/... targets are affected
    has_props_frontend: "true" if //props/frontend/... targets are affected

    # Release mode only:
    release_needed: "true" or "false"
    base_sha: The last release commit SHA (empty if first release)
    reason: Human-readable explanation
"""

from tools.ci.bazel_diff_lib import main

if __name__ == "__main__":
    main()
