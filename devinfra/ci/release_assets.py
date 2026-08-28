"""Resolve where a `bb remote build` landed each release asset, from its own stream.

artifact_targets.json and skills/skills_registry.json name each asset's Bazel
target and released `filename`, but not where Bazel writes the file: the output
path encodes configuration (`k8-fastbuild` vs debundle's `k8-opt`) and bzlmod's
mangled external-repo directories, so a path column in the SSOT went stale on
every toolchain or flag move. The build itself already says where everything
went — this reads the build event stream of the invocation that just ran and
prints each asset's path as `bb remote build` materialized it on the GitHub
runner, under `bb-out/`.

Everything here fails loud, unlike plan_releases.py, the fail-open half: this
runs *after* the build, in the job about to publish (release-paths) or to feed
the nix imports gate (nix-overrides), where a target whose artifact the stream
does not name, an ambiguous artifact, or a built basename contradicting the
SSOT's `filename` is a configuration error to fix, never a row to skip.

Runs as bare `python3 -m` on a GitHub Actions runner, so it stays on the
standard library.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping
from pathlib import Path

from devinfra.ci.bes import Output, artifact_output, merge, read
from devinfra.ci.plan_releases import Release, load_releases

#: `bb remote build` materializes an output the stream reports at `path` here.
BB_OUT_PREFIX = "bb-out/"


def resolve_asset(by_label: Mapping[str, list[Output]], target: str, filename: str) -> str:
    """The `bb-out/` path of `target`'s one artifact, checked against `filename`.

    The filename check is what keeps the SSOT honest: the released asset's name
    (and so every pin's download URL) is the built file's basename, so a
    mismatch means the registry no longer describes what the build produces.
    """
    artifact = artifact_output(by_label, target)
    if isinstance(artifact, str):
        raise SystemExit(artifact)
    if (built := Path(artifact.path).name) != filename:
        raise SystemExit(f"{target} built {built!r} but the registry says filename={filename!r}")
    return BB_OUT_PREFIX + artifact.path


def release_paths(release: Release, by_label: Mapping[str, list[Output]]) -> list[str]:
    return [resolve_asset(by_label, target, filename) for target, filename in release.assets]


def nix_override_rows(artifact_targets: Path, by_label: Mapping[str, list[Output]]) -> list[tuple[str, str]]:
    """(pin name, bb-out path) for every pin whose release is in the nix PR gate."""
    doc = json.loads(artifact_targets.read_text())
    return [
        (name, resolve_asset(by_label, pin["target"], pin["filename"]))
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
    commands.add_parser(
        "nix-overrides", parents=[common], help="print `pin<TAB>bb-out path` for every nixPackage-gated pin"
    )

    args = parser.parse_args(argv)
    merged = merge(read(invocation_id) for invocation_id in args.invocations.split(",") if invocation_id)
    if merged is None:
        raise SystemExit("--invocations is empty; the build step must expose its bazel_invocation_ids")
    by_label = merged.by_label()

    if args.command == "release-paths":
        releases = [r for r in load_releases(args.artifact_targets, args.skills_registry) if r.pkg == args.release]
        if not releases:
            raise SystemExit(f"no release named {args.release!r} in the SSOT")
        (release,) = releases
        print("\n".join(release_paths(release, by_label)))
    else:
        rows = nix_override_rows(args.artifact_targets, by_label)
        sys.stdout.writelines(f"{name}\t{path}\n" for name, path in rows)


if __name__ == "__main__":
    main()
