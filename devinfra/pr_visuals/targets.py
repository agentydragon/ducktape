"""What an invocation's targets did, read from BuildBuddy rather than scraped from a console.

`bbapi target <invocation> --json` is the structured form of the per-target lines Bazel prints,
and it carries what those lines do not: how many shards a target ran on, and the wall-clock
window its runs occupied. That window is the number `size` has to cover, because Bazel's timeout
applies to each shard rather than to their sum.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from pydantic import BaseModel, Field

from devinfra.pr_visuals.artifacts import Runner


class TestSummary(BaseModel):
    """The test half of a target's row. A target that was only built has none."""

    # Absent for an unsharded target, which is one shard.
    shard_count: int = Field(default=1, alias="shardCount")
    first_start_time: datetime = Field(alias="firstStartTime")
    last_stop_time: datetime = Field(alias="lastStopTime")

    @property
    def wall_time(self) -> timedelta:
        """How long the target occupied with its shards running in parallel.

        This rather than the listing's own `totalRunDuration`, which sums them: four 10s shards
        report 40s there, while each one only ever had to finish inside the timeout alone.
        """
        return self.last_stop_time - self.first_start_time


class TargetMetadata(BaseModel):
    label: str


class Target(BaseModel):
    """One row of the listing, which is not always a run.

    A target contributes several rows: one naming its output files, one for its build, and -- if
    it is a test -- one for its test run. Only the last has both a status and a test summary, so
    a row carrying neither is one of the others rather than a malformed run.
    """

    metadata: TargetMetadata
    # BuildBuddy's own vocabulary (PASSED, FAILED, BUILT, ...), carried verbatim because nothing
    # here dispatches on it -- it is reported so a reader can see a run that did not pass, while
    # a status BuildBuddy adds tomorrow prints rather than failing a measurement run.
    status: str | None = None
    test_summary: TestSummary | None = Field(default=None, alias="testSummary")


class TargetGroup(BaseModel):
    # bbapi omits the key rather than sending an empty list.
    targets: list[Target] = []


class TargetListing(BaseModel):
    target_groups: list[TargetGroup] = Field(default=[], alias="targetGroups")


@dataclass(frozen=True)
class TestRun:
    """One target's test run in one invocation: what it was, how it ended, and what it cost."""

    label: str
    status: str
    summary: TestSummary


def _listing(invocation: str, *extra: str, bbapi: Path, run: Runner) -> list[TestRun]:
    result = run([bbapi, "target", invocation, "--json", *extra], check=False, text=True, capture_output=True)
    if result.returncode != 0:
        raise RuntimeError(f"listing targets of {invocation} failed: {result.stderr.strip()}")
    listing = TargetListing.model_validate_json(result.stdout)
    return [
        TestRun(target.metadata.label, target.status, target.test_summary)
        for group in listing.target_groups
        for target in group.targets
        if target.test_summary is not None and target.status is not None
    ]


def list_test_runs(
    invocation: str, expected: list[str], *, bbapi: Path = Path("bbapi"), run: Runner = subprocess.run
) -> list[TestRun]:
    """Every target in `invocation` that ran as a test, including the ones the listing truncates.

    The listing also carries each target's file and build rows; the test summary is what marks
    the one row that actually ran, and the others have nothing to say about duration.

    Gotcha: the bulk listing is capped -- it answered with exactly twelve targets per group for a
    fifteen-target sweep, and offered no page token to say so. The missing three were simply the
    last alphabetically, so a report built on it alone silently omits whatever sorts late. Asking
    for those by `--label` returns them, so `expected` is what turns a truncated answer into a
    complete one.
    """
    runs = _listing(invocation, bbapi=bbapi, run=run)
    missing = sorted(set(expected) - {test_run.label for test_run in runs})
    for label in missing:
        runs.extend(_listing(invocation, "--label", label, bbapi=bbapi, run=run))
    return runs
