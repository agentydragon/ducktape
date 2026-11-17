"""Load BuildBuddy evidence for release and image publication planners.

Output files come from normal build invocations. Test verdicts come separately
from the race-enabled test invocation, verified against the current push workflow.
"""

from __future__ import annotations

import dataclasses
import os
import sys
from collections.abc import Callable

from devinfra.buildbuddy.ci.bes import BuildBuddyError, Invocation, merge, read
from devinfra.ci.publication_test_verdicts import TestEvidence, effective_rbe_image, verify


@dataclasses.dataclass(frozen=True)
class PublicationEvidence:
    outputs: Invocation | None
    tests: TestEvidence


def _final_test_invocation(initial_id: str, read_invocation: Callable[[str], Invocation]) -> Invocation:
    """Follow cache retries within the original command's structured runner lineage.

    Bazel changes Started.uuid on a cache retry while retaining the original
    --invocation_id option. The runner records child configuration in start order.
    Missing or contradictory lineage retains the publication job's test gate.
    """
    initial = read_invocation(initial_id)
    if not initial.successful and initial.exit_code_name is None:
        raise BuildBuddyError("test invocation has no terminal exit code; a superseding retry cannot be excluded")
    if initial.exit_code_name != "REMOTE_CACHE_EVICTED":
        return initial
    if not initial.finished or initial.invocation_id != initial_id or initial.parent_invocation_id is None:
        raise BuildBuddyError("cache-evicted test invocation has incomplete identity or runner evidence")
    requested_id_option = f"--invocation_id={initial_id}"
    if initial.explicit_cmd_line is None or initial.explicit_cmd_line.count(requested_id_option) != 1:
        raise BuildBuddyError("cache-evicted test invocation does not identify its requested invocation")
    parent = read_invocation(initial.parent_invocation_id)
    if not parent.finished or parent.invocation_id != initial.parent_invocation_id:
        raise BuildBuddyError("test runner has no unambiguous terminal invocation")
    children = parent.child_invocation_ids
    if len(set(children)) != len(children) or children.count(initial_id) != 1:
        raise BuildBuddyError("test runner does not uniquely configure the initial test invocation")
    latest = initial
    for identifier in children[children.index(initial_id) + 1 :]:
        candidate = read_invocation(identifier)
        if candidate.command != initial.command:
            if candidate.command is None:
                raise BuildBuddyError(f"runner child {identifier} has no unambiguous command")
            continue
        if candidate.explicit_cmd_line is None:
            raise BuildBuddyError(f"runner child {identifier} has no unambiguous command line")
        if requested_id_option not in candidate.explicit_cmd_line:
            continue
        if not latest.finished or latest.exit_code_name != "REMOTE_CACHE_EVICTED":
            raise BuildBuddyError("runner configured another test attempt after a non-cache terminal result")
        if (
            candidate.invocation_id != identifier
            or candidate.parent_invocation_id != initial.parent_invocation_id
            or candidate.command != initial.command
            or candidate.commit_sha != initial.commit_sha
            or candidate.requested_patterns != initial.requested_patterns
            or candidate.explicit_cmd_line != initial.explicit_cmd_line
        ):
            raise BuildBuddyError(f"test retry {identifier} contradicts the original command provenance")
        latest = candidate
    if (
        latest is initial
        or not latest.finished
        or latest.exit_code_name is None
        or latest.exit_code_name == "REMOTE_CACHE_EVICTED"
    ):
        raise BuildBuddyError("test runner has no terminal non-cache retry result")
    print(f"::notice::Publication test evidence uses {latest.invocation_id} (initial {initial_id})", file=sys.stderr)
    return latest


def load(
    invocation_ids: list[str], *, test_invocation_id: str, source_commit: str, rbe_container_image: str
) -> PublicationEvidence:
    """Read each invocation once; unavailable evidence retains publication work."""
    if not invocation_ids:
        print("::warning::no bazel-ci invocation to read; retaining all publication candidates", file=sys.stderr)
    readable: dict[str, Invocation] = {}

    def read_invocation(identifier: str) -> Invocation:
        if identifier not in readable:
            readable[identifier] = read(identifier)
        return readable[identifier]

    requested_ids = [*invocation_ids, *([test_invocation_id] if test_invocation_id else [])]
    for invocation_id in dict.fromkeys(requested_ids):
        try:
            read_invocation(invocation_id)
        except (BuildBuddyError, KeyError, TypeError, ValueError, IndexError) as error:
            print(
                f"::warning::could not read invocation {invocation_id} ({error}); retaining publication candidates",
                file=sys.stderr,
            )
    tests = readable.get(test_invocation_id)
    if tests is not None:
        try:
            tests = _final_test_invocation(test_invocation_id, read_invocation)
        except (BuildBuddyError, KeyError, TypeError, ValueError, IndexError) as error:
            print(
                f"::warning::could not resolve final bazel-ci test attempt ({error}); retaining test gates",
                file=sys.stderr,
            )
            tests = None
    return PublicationEvidence(
        outputs=merge(
            readable[identifier]
            for identifier in dict.fromkeys(invocation_ids)
            if identifier != test_invocation_id and identifier in readable
        ),
        tests=verify(
            tests,
            invocation_id_value=test_invocation_id,
            source_commit=source_commit,
            current_commit=os.environ.get("GITHUB_SHA", ""),
            run_id=os.environ.get("GITHUB_RUN_ID", ""),
            run_attempt=os.environ.get("GITHUB_RUN_ATTEMPT", ""),
            event_name=os.environ.get("GITHUB_EVENT_NAME", ""),
            rbe_image=effective_rbe_image(rbe_container_image),
        ),
    )
