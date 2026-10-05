"""Bazel test sharding for pytest: collected item `i` runs on shard `i % TEST_TOTAL_SHARDS`.

`py_test(shard_count = ...)` loads this plugin (devinfra/python/defs.bzl); pytest_bazel turns Bazel's
shard environment into the `--shard-id`/`--num-shards` flags it reads.

Deviation from pytest-shard, the plugin pytest_bazel expects: that one places each item by a hash of
its node ID, so shard sizes scatter -- 27 items over 4 shards can land 8/9/5/5. Placing by position
keeps every shard within one item of the others.

Deviation from running the filter alone: `pytest_bazel` reads "nothing collected" under a filter as success,
so a `--test_filter` naming no case would pass on every shard having run nothing. A filter that leaves nothing
before sharding fails instead; one that matches a case owned by another shard still passes on this one.
"""

import os
from pathlib import Path

import pytest


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("sharding")
    group.addoption("--shard-id", type=int, default=0, help="This shard's index, from TEST_SHARD_INDEX.")
    group.addoption("--num-shards", type=int, default=1, help="The shard count, from TEST_TOTAL_SHARDS.")


def pytest_configure(config: pytest.Config) -> None:
    # Bazel fails a sharded test whose runner does not touch this file to advertise support, and
    # pytest_bazel touches it only when it can import pytest-shard.
    if status_file := os.environ.get("TEST_SHARD_STATUS_FILE"):
        Path(status_file).touch()


@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    # trylast: shard what `-k` (`--test_filter`) left, so a filtered item runs on whichever shard
    # it lands and the others pass empty.
    if not items and (keyword := config.getoption("keyword")):
        pytest.exit(f"{keyword=} matched no test", returncode=pytest.ExitCode.USAGE_ERROR)
    items[:] = items[config.getoption("shard_id") :: config.getoption("num_shards")]


def pytest_report_collectionfinish(config: pytest.Config, items: list[pytest.Item]) -> str:
    return f"Shard {config.getoption('shard_id') + 1} of {config.getoption('num_shards')}: {len(items)} items"
