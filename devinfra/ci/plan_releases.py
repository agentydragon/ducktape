"""Decide which releases actually need publishing, before the matrix fans out.

`.github/workflows/release.yml` gave every release its own matrix job to check out
the repo, install the devshell, build its artifacts and hash them — almost always to
discover the content was unchanged and the tag already existed. Fifty runner slots
per merge to publish, typically, nothing. See devinfra/ci/debug/ci_queue_saturation.md.

This runs once in the matrix job instead: one `bb remote build` materializes every
default-config release's artifacts, one pass over the existing tags decides which
moved, and the matrix carries only those.

Two properties this deliberately keeps:

  Tags never churn. The identity is `release_content_hash.py`, byte for byte what
  `.github/actions/release-artifact` already computes. A different hash here would
  republish all fifty packages once.

  It fails open. A missing artifact, an unreadable file, a `gh` hiccup — anything
  short of proof that the release is unchanged — keeps the row in the matrix, where
  the per-release job does the same check properly and errors loudly. Wrongly
  including a row costs one job; wrongly excluding one silently drops a release.

Rows carrying custom `bazelFlags` (today only `debundle`, at `-c opt`) can't share
the plan build's configuration, so they skip the fast path and always stay in the
matrix — their own job checks and skips exactly as before.

Runs as bare `python3` on a GitHub Actions runner, so it stays on the standard
library. `gh` is preinstalled there.

Subcommands:
  targets  print the Bazel targets to hand to a single `bb remote build`
  plan     hash what that built, diff against published tags, emit matrix.include
"""

from __future__ import annotations

import argparse
import concurrent.futures
import dataclasses
import json
import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

from devinfra.ci.release_content_hash import release_content_hash

TAG_HASH_LENGTH = 12


@dataclasses.dataclass(frozen=True)
class Release:
    """One row of the release matrix. Field names are the matrix keys release.yml reads."""

    pkg: str
    targets: str
    outputs: str
    tests: str
    bazel_flags: str
    release_metadata: str
    metadata_platform: str

    @property
    def uses_default_config(self) -> bool:
        """Whether this row builds under the same Bazel config as the plan build."""
        return not self.bazel_flags

    @property
    def output_paths(self) -> list[Path]:
        return [Path(p) for p in self.outputs.split()]

    def as_matrix_row(self) -> dict[str, str]:
        return dataclasses.asdict(self)


def load_releases(artifact_targets: Path, skills_registry: Path) -> list[Release]:
    """Every release row, from the two SSOT files release.yml has always read."""
    doc = json.loads(artifact_targets.read_text())
    pins = doc["pins"]
    rows = [
        Release(
            pkg=name,
            targets=" ".join(pin["target"] for pin in owned),
            outputs=" ".join(pin["output"] for pin in owned),
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
            outputs=skill["output"],
            tests="",
            bazel_flags="",
            release_metadata="false",
            metadata_platform="",
        )
        for skill in skills
    )
    return rows


def content_tag(pkg: str, artifacts: list[Path]) -> str:
    return f"{pkg}-{release_content_hash(artifacts)[:TAG_HASH_LENGTH]}"


def is_published(release: Release, root: Path, tag_exists: Callable[[str], bool]) -> bool:
    """True only when this release's exact content is already published.

    Every uncertain path returns False so the row stays in the matrix.
    """
    if not release.uses_default_config:
        return False
    artifacts = [root / path for path in release.output_paths]
    missing = [str(path) for path in artifacts if not path.is_file()]
    if missing:
        print(f"::warning::{release.pkg}: artifact not found ({missing}); releasing it anyway", file=sys.stderr)
        return False
    try:
        return tag_exists(content_tag(release.pkg, artifacts))
    except (OSError, ValueError, subprocess.SubprocessError) as e:
        # Hashing or the tag lookup failed. Keep the row rather than drop a release;
        # its own job runs the same check and fails loudly if something is really wrong.
        print(f"::warning::{release.pkg}: could not check published tags ({e}); releasing it anyway", file=sys.stderr)
        return False


def gh_tag_exists(tag: str) -> bool:
    """Whether a GitHub release exists for `tag` — the same check release-artifact makes."""
    result = subprocess.run(["gh", "release", "view", tag], check=False, capture_output=True, text=True)
    return result.returncode == 0


def plan(
    releases: list[Release], root: Path, tag_exists: Callable[[str], bool], workers: int
) -> list[tuple[Release, bool]]:
    """Pair every release with whether it is already published. Tag lookups dominate."""
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        published = pool.map(lambda r: is_published(r, root, tag_exists), releases)
    return list(zip(releases, published, strict=True))


def matrix_include(decided: list[tuple[Release, bool]]) -> list[dict[str, str]]:
    return [release.as_matrix_row() for release, published in decided if not published]


def _write_github_output(**values: str) -> None:
    if not (path := os.environ.get("GITHUB_OUTPUT")):
        return
    with Path(path).open("a") as f:
        f.writelines(f"{key}={value}\n" for key, value in values.items())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact-targets", type=Path, default=Path("devinfra/ci/artifact_targets.json"))
    parser.add_argument("--skills-registry", type=Path, default=Path("skills/skills_registry.json"))
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("targets", help="print the default-config targets for one bb remote build")
    plan_parser = subparsers.add_parser("plan", help="emit matrix.include for the releases that moved")
    plan_parser.add_argument("--root", type=Path, default=Path(), help="repo root holding bb-out/")
    plan_parser.add_argument("--workers", type=int, default=8, help="concurrent tag lookups")
    plan_parser.add_argument(
        "--commit-subject",
        default="",
        help="subject of the commit being released; a [skip ci] subject releases nothing",
    )

    args = parser.parse_args(argv)
    releases = load_releases(args.artifact_targets, args.skills_registry)

    if args.command == "targets":
        print(" ".join(r.targets for r in releases if r.uses_default_config))
        return 0

    if "[skip ci]" in args.commit_subject:
        print("Commit is marked [skip ci]; releasing nothing.", file=sys.stderr)
        _write_github_output(matrix=json.dumps({"include": []}), count="0")
        return 0

    decided = plan(releases, args.root, gh_tag_exists, args.workers)
    include = matrix_include(decided)

    for release, published in sorted(decided, key=lambda d: d[0].pkg):
        print(f"{'skip' if published else 'RELEASE':>7}  {release.pkg}", file=sys.stderr)
    print(f"\n{len(include)} of {len(decided)} releases need publishing.", file=sys.stderr)

    _write_github_output(matrix=json.dumps({"include": include}), count=str(len(include)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
