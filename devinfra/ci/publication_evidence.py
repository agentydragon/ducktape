"""Load build-output evidence for release and image publication planners."""

import sys

from devinfra.buildbuddy.ci.bes import BuildBuddyError, Invocation, merge, read


def load(invocation_ids: list[str]) -> Invocation | None:
    """Read each build once; unavailable evidence retains publication work."""
    if not invocation_ids:
        print("::warning::no bazel-ci invocation to read; retaining all publication candidates", file=sys.stderr)
    readable = []
    for invocation_id in dict.fromkeys(invocation_ids):
        try:
            readable.append(read(invocation_id))
        except (BuildBuddyError, KeyError, TypeError, ValueError, IndexError) as error:
            print(
                f"::warning::could not read invocation {invocation_id} ({error}); retaining publication candidates",
                file=sys.stderr,
            )
    return merge(readable)
