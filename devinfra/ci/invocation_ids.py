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


def invocation_id(*, run_id: str, attempt: str, role: str, scope: str = "") -> uuid.UUID:
    identity = f"ducktape/bazel-ci/{run_id}/{attempt}/{role}"
    if scope:
        identity = f"{identity}/{scope}"
    return uuid.uuid5(_NAMESPACE, identity)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--attempt", required=True)
    parser.add_argument(
        "--role",
        required=True,
        choices=["test", "build", "race", "nix-wheel-build", "release-unstamped", "release-stamped"],
    )
    parser.add_argument("--scope", default="", help="Optional scope to separate parallel invocations of one role")
    args = parser.parse_args()
    if args.role.startswith("release-") and not args.scope:
        parser.error("--scope is required for release invocations")
    print(invocation_id(run_id=args.run_id, attempt=args.attempt, role=args.role, scope=args.scope))


if __name__ == "__main__":
    main()
