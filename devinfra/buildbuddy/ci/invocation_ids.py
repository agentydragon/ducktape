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
import dataclasses
import uuid
from pathlib import Path

# Arbitrary fixed namespace; only its stability matters.
_NAMESPACE = uuid.UUID("6f9619ff-8b86-d011-b42d-00c04fc964ff")


@dataclasses.dataclass(frozen=True)
class BazelCIRole:
    name: str
    publication_outputs: bool


TEST_ROLE = BazelCIRole("test", publication_outputs=False)
BUILD_ROLE = BazelCIRole("build", publication_outputs=True)
BAZEL_CI_ROLES = (TEST_ROLE, BUILD_ROLE)
BAZEL_CI_PUBLICATION_ROLES = tuple(role for role in BAZEL_CI_ROLES if role.publication_outputs)
_OTHER_INVOCATION_ROLES = ("nix-wheel-build", "release-unstamped", "release-stamped")


def invocation_id(*, run_id: str, attempt: str, role: str, scope: str = "") -> uuid.UUID:
    identity = f"ducktape/bazel-ci/{run_id}/{attempt}/{role}"
    if scope:
        identity = f"{identity}/{scope}"
    return uuid.uuid5(_NAMESPACE, identity)


def bazel_ci_output_values(*, run_id: str, attempt: str) -> dict[str, str]:
    """Derive GitHub step outputs for Bazel CI from its role definitions."""
    ids = {role.name: str(invocation_id(run_id=run_id, attempt=attempt, role=role.name)) for role in BAZEL_CI_ROLES}
    return {
        **ids,
        "all_invocation_ids": ",".join(ids[role.name] for role in BAZEL_CI_ROLES),
        "publication": ",".join(ids[role.name] for role in BAZEL_CI_PUBLICATION_ROLES),
        "linkage_roles": ",".join(role.name for role in BAZEL_CI_ROLES),
        "env_overrides": "\n".join(f"{role.name.upper()}_INVOCATION_ID={ids[role.name]}" for role in BAZEL_CI_ROLES),
    }


def write_bazel_ci_outputs(path: Path, *, run_id: str, attempt: str) -> None:
    """Append the role-derived outputs in GitHub Actions' step-output format."""
    values = bazel_ci_output_values(run_id=run_id, attempt=attempt)
    with path.open("a") as output:
        for name, value in values.items():
            if "\n" in value:
                delimiter = "BAZEL_CI_ENV_OVERRIDES_EOF"
                output.write(f"{name}<<{delimiter}\n{value}\n{delimiter}\n")
            else:
                output.write(f"{name}={value}\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--attempt", required=True)
    parser.add_argument("--role", choices=[*(role.name for role in BAZEL_CI_ROLES), *_OTHER_INVOCATION_ROLES])
    parser.add_argument("--scope", default="", help="Optional scope to separate parallel invocations of one role")
    parser.add_argument(
        "--bazel-ci-outputs",
        type=Path,
        metavar="GITHUB_OUTPUT",
        help="Write all role-derived Bazel CI step outputs to this GitHub Actions file",
    )
    args = parser.parse_args()
    if bool(args.role) == bool(args.bazel_ci_outputs):
        parser.error("supply exactly one of --role or --bazel-ci-outputs")
    if args.bazel_ci_outputs:
        write_bazel_ci_outputs(args.bazel_ci_outputs, run_id=args.run_id, attempt=args.attempt)
        return
    if args.role.startswith("release-") and not args.scope:
        parser.error("--scope is required for release invocations")
    print(invocation_id(run_id=args.run_id, attempt=args.attempt, role=args.role, scope=args.scope))


if __name__ == "__main__":
    main()
