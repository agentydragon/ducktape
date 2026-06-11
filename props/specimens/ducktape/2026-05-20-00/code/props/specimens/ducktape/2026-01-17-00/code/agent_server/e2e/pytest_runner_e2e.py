"""Pytest runner for e2e tests (requires Playwright browsers).

TODO: Investigate using rules_python's py_pytest_main from
@rules_python//python/private/pytest:defs.bzl as an alternative to
this manual runner. It may handle Bazel integration (XML output,
sharding, etc.) automatically.
"""

import os
import sys
from pathlib import Path

import pytest

if __name__ == "__main__":
    pytest_args = [
        "--import-mode=importlib",
        "--ignore=external",
        "-v",
        # pytest-asyncio configuration
        "-o",
        "asyncio_mode=auto",
        "-o",
        "asyncio_default_fixture_loop_scope=function",
        "agent_server/e2e",
    ]

    # Handle Bazel's XML output
    if os.environ.get("XML_OUTPUT_FILE"):
        pytest_args.append(f"--junitxml={os.environ['XML_OUTPUT_FILE']}")

    # Handle test sharding
    if os.environ.get("TEST_SHARD_INDEX") and os.environ.get("TEST_TOTAL_SHARDS"):
        pytest_args.append(f"--shard-id={os.environ['TEST_SHARD_INDEX']}")
        pytest_args.append(f"--num-shards={os.environ['TEST_TOTAL_SHARDS']}")
        if os.environ.get("TEST_SHARD_STATUS_FILE"):
            Path(os.environ["TEST_SHARD_STATUS_FILE"]).touch()

    # Handle test filtering
    test_filter = os.environ.get("TESTBRIDGE_TEST_ONLY")
    if test_filter:
        pytest_args.append(f"-k={test_filter}")

    pytest_args.extend(sys.argv[1:])

    sys.exit(pytest.main(pytest_args))
