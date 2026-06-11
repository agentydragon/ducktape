"""Pytest runner shim for Bazel integration.

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
        "--ignore=agent_server/e2e",  # e2e tests run in separate target
        "-v",
        # pytest-asyncio configuration (normally in pyproject.toml but not found under Bazel)
        "-o",
        "asyncio_mode=auto",
        "-o",
        "asyncio_default_fixture_loop_scope=function",
        # Disable anyio plugin which can conflict with pytest-asyncio's event loop management
        "-p",
        "no:anyio",
        "agent_server",
    ]

    # Handle Bazel's XML output
    if os.environ.get("XML_OUTPUT_FILE"):
        pytest_args.append(f"--junitxml={os.environ['XML_OUTPUT_FILE']}")

    # Handle test sharding (requires pytest-shard)
    if os.environ.get("TEST_SHARD_INDEX") and os.environ.get("TEST_TOTAL_SHARDS"):
        pytest_args.append(f"--shard-id={os.environ['TEST_SHARD_INDEX']}")
        pytest_args.append(f"--num-shards={os.environ['TEST_TOTAL_SHARDS']}")
        if os.environ.get("TEST_SHARD_STATUS_FILE"):
            Path(os.environ["TEST_SHARD_STATUS_FILE"]).touch()

    # Handle test filtering via TESTBRIDGE_TEST_ONLY (set by --test_filter)
    test_filter = os.environ.get("TESTBRIDGE_TEST_ONLY")
    if test_filter:
        pytest_args.append(f"-k={test_filter}")

    # Add any extra args passed via command line
    pytest_args.extend(sys.argv[1:])

    sys.exit(pytest.main(pytest_args))
