"""Generic pre-commit entry point for ducktape wheel.

Dispatches to individual pre-commit checks. Currently runs:
- enforce-bazel-tests: verify affected Bazel tests are cached/passing
"""

from __future__ import annotations

import sys

from devinfra.precommit.enforce_bazel_tests.enforce_bazel_tests import main as enforce_bazel_tests_main


def main() -> int:
    return enforce_bazel_tests_main()


if __name__ == "__main__":
    sys.exit(main())
