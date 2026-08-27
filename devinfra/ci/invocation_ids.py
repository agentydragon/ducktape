"""Deterministic BuildBuddy invocation IDs for a GitHub Actions CI run.

Bazel accepts `--invocation_id`, so CI names its invocations up front rather than
discovering them afterwards. Both sides of the handoff derive the same value from
the run's identity: `bazel-ci.yml` passes it to Bazel, and `devinfra/pr_visuals`
recomputes it from `workflow_run.id` to find the artifacts.

Gotcha: this is the only reason a *superseded* run's visuals are reachable at
all. Discovering the ID from `bb remote`'s output — whether by parsing its log or
by its `-invocation_id_file` — happens after the command returns, so cancellation
loses it while the invocation itself sits complete in BuildBuddy.

BuildBuddy merges two invocations that claim one ID rather than rejecting the
second, so `attempt` is part of the key: a re-run must not fold into the run it
replaces.
"""

import argparse
import uuid

# Arbitrary fixed namespace; only its stability matters.
_NAMESPACE = uuid.UUID("6f9619ff-8b86-d011-b42d-00c04fc964ff")


def invocation_id(*, run_id: str, attempt: str, role: str) -> uuid.UUID:
    return uuid.uuid5(_NAMESPACE, f"ducktape/bazel-ci/{run_id}/{attempt}/{role}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--attempt", required=True)
    parser.add_argument("--role", required=True, choices=["test", "build"])
    args = parser.parse_args()
    print(invocation_id(run_id=args.run_id, attempt=args.attempt, role=args.role))


if __name__ == "__main__":
    main()
