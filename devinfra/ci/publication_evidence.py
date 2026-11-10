"""Load BuildBuddy evidence for release and image publication planners.

Output files come from normal build invocations. Test verdicts come separately
from the race-enabled test invocation, verified against the current push workflow.
"""

from __future__ import annotations

import dataclasses
import os
import sys

from devinfra.buildbuddy.ci.bes import BuildBuddyError, Invocation, merge, read
from devinfra.ci.publication_test_verdicts import TestEvidence, effective_rbe_image, verify


@dataclasses.dataclass(frozen=True)
class PublicationEvidence:
    outputs: Invocation | None
    tests: TestEvidence


def load(
    invocation_ids: list[str], *, test_invocation_id: str, source_commit: str, rbe_container_image: str
) -> PublicationEvidence:
    """Read each invocation once; unavailable evidence retains publication work."""
    if not invocation_ids:
        print("::warning::no bazel-ci invocation to read; retaining all publication candidates", file=sys.stderr)
    readable: dict[str, Invocation] = {}
    requested_ids = [*invocation_ids, *([test_invocation_id] if test_invocation_id else [])]
    for invocation_id in dict.fromkeys(requested_ids):
        try:
            readable[invocation_id] = read(invocation_id)
        except (BuildBuddyError, KeyError, TypeError, ValueError, IndexError) as error:
            print(
                f"::warning::could not read invocation {invocation_id} ({error}); retaining publication candidates",
                file=sys.stderr,
            )
    return PublicationEvidence(
        outputs=merge(
            readable[identifier]
            for identifier in dict.fromkeys(invocation_ids)
            if identifier != test_invocation_id and identifier in readable
        ),
        tests=verify(
            readable.get(test_invocation_id),
            invocation_id_value=test_invocation_id,
            source_commit=source_commit,
            current_commit=os.environ.get("GITHUB_SHA", ""),
            run_id=os.environ.get("GITHUB_RUN_ID", ""),
            run_attempt=os.environ.get("GITHUB_RUN_ATTEMPT", ""),
            event_name=os.environ.get("GITHUB_EVENT_NAME", ""),
            rbe_image=effective_rbe_image(rbe_container_image),
        ),
    )
