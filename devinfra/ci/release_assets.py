"""Load the release roster and resolve asset paths from the build that produced them.

The registries keep each target and released filename. BES supplies the output
path; its basename must match that filename, which is part of the pin URL
contract. Only the target's default, non-aspect output is an asset.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
from collections.abc import Mapping
from pathlib import Path

from devinfra.buildbuddy.ci.bes import Output, merge, read

#: `bb remote build` materializes an output the stream reports at `path` here.
BB_OUT_PREFIX = "bb-out/"


@dataclasses.dataclass(frozen=True)
class Release:
    """One row of the release matrix.

    Field names are the matrix keys release.yml reads — except `filenames`,
    which both the planner and publishing job use.
    """

    pkg: str
    targets: str
    #: Each target's released asset name, aligned with `targets`. The name is a
    #: contract: sync_pins.py derives every pin's download URL from it, and
    #: release_assets.py refuses a built file that carries a different name.
    filenames: str
    tests: str
    bazel_flags: str
    release_metadata: str
    metadata_platform: str

    @property
    def uses_default_config(self) -> bool:
        """Whether this row was built under the same Bazel config as bazel-ci."""
        return not self.bazel_flags

    @property
    def assets(self) -> list[tuple[str, str]]:
        """(target, filename) pairs, aligned by position."""
        return list(zip(self.targets.split(), self.filenames.split(), strict=True))

    def as_matrix_row(self) -> dict[str, str]:
        row = dataclasses.asdict(self)
        del row["filenames"]
        return row


def load_releases(artifact_targets: Path, skills_registry: Path) -> list[Release]:
    """Every release row, from the two SSOT files release.yml has always read."""
    doc = json.loads(artifact_targets.read_text())
    pins = doc["pins"]
    rows = [
        Release(
            pkg=name,
            targets=" ".join(pin["target"] for pin in owned),
            filenames=" ".join(pin["filename"] for pin in owned),
            tests=entry.get("tests", ""),
            bazel_flags=entry.get("bazelFlags", ""),
            release_metadata=str(entry.get("releaseMetadata", False)).lower(),
            metadata_platform=entry.get("metadataPlatform", ""),
        )
        for name, entry in doc["releases"].items()
        if (owned := [pin for pin in pins.values() if pin["release"] == name])
    ]
    # One release per deployable skill; skills/skills_registry.json is the SSOT.
    # Their `<name>_frontmatter_test` already runs in bazel-ci, so tests stays empty.
    skills = json.loads(skills_registry.read_text())["skills"]
    rows.extend(
        Release(
            pkg=skill["pkg"],
            targets=skill["target"],
            filenames=skill["filename"],
            tests="",
            bazel_flags="",
            release_metadata="false",
            metadata_platform="",
        )
        for skill in skills
    )
    return rows


def resolve_asset_output(by_label: Mapping[str, list[Output]], target: str, filename: str) -> Output:
    """Return the invocation-reported default output, checking its basename."""
    candidates = sorted(
        {output for output in by_label.get(target, []) if not output.aspect and output.output_group == "default"},
        key=lambda output: output.path,
    )
    if not candidates:
        raise ValueError(f"{target}: not reported by the build")
    if len(candidates) > 1:
        paths = ", ".join(output.path for output in candidates)
        raise ValueError(
            f"{target}: reports {len(candidates)} default outputs ({paths}); an artifact target must publish one"
        )
    artifact = candidates[0]
    if (built := Path(artifact.path).name) != filename:
        raise ValueError(f"{target} built {built!r} but the registry says filename={filename!r}")
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
    try:
        paths = release_paths(release, by_label)
    except ValueError as error:
        raise SystemExit(str(error)) from error
    print("\n".join(paths))


if __name__ == "__main__":
    main()
