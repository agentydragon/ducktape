"""Materialize a finished Bazel invocation's outputs on the GitHub runner.

`bb remote build` downloads only an arbitrary subset of a large request's outputs,
so jobs that need every file use its build event stream to fetch the content by
CAS URI.
"""

import argparse
from pathlib import Path

from devinfra.ci.bes import BuildBuddyError, fetch_blob, merge, read

BB_OUT_PREFIX = "bb-out/"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--invocations", required=True, help="comma-separated BuildBuddy invocation ids")
    parser.add_argument("outputs", nargs="+", type=Path, help="paths resolved from the invocation event stream")
    args = parser.parse_args()
    invocation = merge(read(i) for i in args.invocations.split(",") if i)
    if invocation is None:
        raise BuildBuddyError("no invocation to read outputs from")
    by_path = invocation.by_path()
    for output in args.outputs:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(fetch_blob(by_path[str(output).removeprefix(BB_OUT_PREFIX)].uri))
        print(f"fetched {output}")


if __name__ == "__main__":
    main()
