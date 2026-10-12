"""Materialize a finished Bazel invocation's outputs on the GitHub runner.

`bb remote build` downloads only an arbitrary subset of a large request's outputs
(measured: 12 of 13 wheels), so a job that needs every file reads it from the build
event stream instead.
"""

import argparse
from pathlib import Path

from devinfra.ci.bes import BuildBuddyError, fetch_blob, merge, read
from devinfra.ci.plan_releases import bes_path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--invocations", required=True, help="comma-separated BuildBuddy invocation ids")
    parser.add_argument("outputs", nargs="+", type=Path, help="paths as artifact_targets.json spells them")
    args = parser.parse_args()
    invocation = merge(read(i) for i in args.invocations.split(",") if i)
    if invocation is None:
        raise BuildBuddyError("no invocation to read outputs from")
    by_path = invocation.by_path()
    for output in args.outputs:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(fetch_blob(by_path[bes_path(str(output))].uri))
        print(f"fetched {output}")


if __name__ == "__main__":
    main()
