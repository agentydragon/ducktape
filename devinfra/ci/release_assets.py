"""Resolve release asset paths from the invocation that built them.

The registries keep each target and released filename. BES supplies the output
path; its basename must match that filename, which is part of the pin URL
contract. Only the target's default, non-aspect output is an asset.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Mapping
from pathlib import Path

from devinfra.buildbuddy.ci.bes import Output, artifact_output, merge, read
from devinfra.ci.plan_releases import Release, load_releases

#: `bb remote build` materializes an output the stream reports at `path` here.
BB_OUT_PREFIX = "bb-out/"


def resolve_asset_output(by_label: Mapping[str, list[Output]], target: str, filename: str) -> Output:
    """Return the invocation-reported default output, checking its basename."""
    artifact = artifact_output(by_label, target)
    if isinstance(artifact, str):
        raise SystemExit(artifact)
    if (built := Path(artifact.path).name) != filename:
        raise SystemExit(f"{target} built {built!r} but the registry says filename={filename!r}")
    return artifact


def resolve_asset(by_label: Mapping[str, list[Output]], target: str, filename: str) -> str:
    """Return the invocation-reported default output under `bb-out/`, checking its basename."""
    return BB_OUT_PREFIX + resolve_asset_output(by_label, target, filename).path


def release_paths(release: Release, by_label: Mapping[str, list[Output]]) -> list[str]:
    return [resolve_asset(by_label, target, filename) for target, filename in release.assets]


def nix_override_assets(artifact_targets: Path, by_label: Mapping[str, list[Output]]) -> list[tuple[str, Output]]:
    """(pin name, default output) for every pin whose release is in the nix PR gate."""
    doc = json.loads(artifact_targets.read_text())
    return [
        (name, resolve_asset_output(by_label, pin["target"], pin["filename"]))
        for name, pin in doc["pins"].items()
        if doc["releases"][pin["release"]].get("nixPackage")
    ]


def main(argv: list[str] | None = None) -> None:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--artifact-targets", type=Path, default=Path("devinfra/ci/artifact_targets.json"))
    common.add_argument(
        "--invocations",
        required=True,
        help="comma-separated invocation ids of the build that produced the assets "
        "(a bb-remote step's bazel_invocation_ids output)",
    )
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    paths_cmd = commands.add_parser(
        "release-paths", parents=[common], help="print one bb-out/ path per asset of --release, in SSOT order"
    )
    paths_cmd.add_argument("--release", required=True, help="release pkg name (a release.yml matrix row's pkg)")
    paths_cmd.add_argument("--skills-registry", type=Path, default=Path("skills/skills_registry.json"))
    args = parser.parse_args(argv)
    merged = merge(read(invocation_id) for invocation_id in args.invocations.split(",") if invocation_id)
    if merged is None:
        raise SystemExit("--invocations is empty; the build step must expose its bazel_invocation_ids")
    by_label = merged.by_label()

    releases = [r for r in load_releases(args.artifact_targets, args.skills_registry) if r.pkg == args.release]
    if not releases:
        raise SystemExit(f"no release named {args.release!r} in the SSOT")
    (release,) = releases
    print("\n".join(release_paths(release, by_label)))


if __name__ == "__main__":
    main()
