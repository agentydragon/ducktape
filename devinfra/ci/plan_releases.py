"""Decide which releases actually need publishing, before the matrix fans out.

`.github/workflows/release.yml` gave every release its own matrix job to check out
the repo, install the devshell, build its artifacts and hash them — almost always to
discover the content was unchanged and the tag already existed. Fifty runner slots
per merge to publish, typically, nothing. See devinfra/ci/debug/ci_queue_saturation.md.

This runs once in the matrix job instead, and builds nothing at all. `bazel-ci`
already built every default-config release on this commit, and BuildBuddy still
holds that invocation's build event stream — including each output's content
digest. So the planner reads the digests, turns them into the tags they would be
published under, and asks which of those tags already exist.

Two properties this deliberately keeps:

  Tags never churn. The identity is `release_content_hash.py`, byte for byte what
  `.github/actions/release-artifact` already computes — a build event stream
  reports each output's sha256, which for a single-asset release *is* the
  identity, and `release_content_hash_from_digests` composes the multi-asset case
  from the same numbers. A different hash here would republish every package once.

  It fails open. No invocation to read, an output the stream never mentions, a
  `gh` hiccup — anything short of proof that the release is unchanged keeps the row
  in the matrix, where the per-release job does the same check properly and errors
  loudly. Wrongly including a row costs one job; wrongly excluding one silently
  drops a release. `release` runs under `always() && !cancelled()`, so a failed or
  skipped bazel-ci must degrade to publishing everything, never to publishing
  nothing.

Rows carrying custom `bazelFlags` (today only `debundle`, at `-c opt`) can't share
the plan build's configuration, so they skip the fast path and always stay in the
matrix — their own job checks and skips exactly as before.

Runs as bare `python3 -m` on a GitHub Actions runner, so it stays on the standard
library. `gh` is preinstalled there.

TODO: gate each release on its own `testSummary` verdict from the same stream,
instead of the per-release job re-running its tests.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import dataclasses
import json
import os
import subprocess
import sys
from collections.abc import Callable, Mapping
from pathlib import Path

from devinfra.ci.bes import BuildBuddyError, Invocation, read
from devinfra.ci.release_content_hash import release_content_hash_from_digests

TAG_HASH_LENGTH = 12

# artifact_targets.json spells every output the way `bb remote build` used to
# materialize it on the GitHub runner, under a `bb-out/` prefix. A build event
# stream reports the same file without it, relative to the workspace.
BB_OUT_PREFIX = "bb-out/"


def bes_path(output: str) -> str:
    """Where `output`, as artifact_targets.json spells it, appears in the stream."""
    return output.removeprefix(BB_OUT_PREFIX)


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
        """Whether this row was built under the same Bazel config as bazel-ci."""
        return not self.bazel_flags

    @property
    def output_paths(self) -> list[str]:
        return self.outputs.split()

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


def content_tag(pkg: str, content_hash: str) -> str:
    return f"{pkg}-{content_hash[:TAG_HASH_LENGTH]}"


def content_hash(release: Release, digests: Mapping[str, str]) -> str | None:
    """This release's published identity, or None if the build did not report it.

    An external-repo target is the expected absence: bazel-ci builds `//...`, which
    does not reach `@ducktape_activitywatch//...`, so aw-importer is never in the
    stream and always takes the slow path.
    """
    assets = []
    for output in release.output_paths:
        digest = digests.get(bes_path(output))
        if not digest:
            return None
        assets.append((Path(output).name, digest))
    return release_content_hash_from_digests(assets)


def is_published(release: Release, digests: Mapping[str, str], tag_exists: Callable[[str], bool]) -> bool:
    """True only when this release's exact content is already published.

    Every uncertain path returns False so the row stays in the matrix.
    """
    if not release.uses_default_config:
        return False
    try:
        digest = content_hash(release, digests)
    except ValueError as e:
        print(f"::warning::{release.pkg}: could not derive its identity ({e}); releasing it anyway", file=sys.stderr)
        return False
    if digest is None:
        print(f"::warning::{release.pkg}: not reported by the build; releasing it anyway", file=sys.stderr)
        return False
    try:
        return tag_exists(content_tag(release.pkg, digest))
    except (OSError, ValueError, subprocess.SubprocessError) as e:
        # The tag lookup failed. Keep the row rather than drop a release; its own
        # job runs the same check and fails loudly if something is really wrong.
        print(f"::warning::{release.pkg}: could not check published tags ({e}); releasing it anyway", file=sys.stderr)
        return False


def gh_tag_exists(tag: str) -> bool:
    """Whether a GitHub release exists for `tag` — the same check release-artifact makes."""
    result = subprocess.run(["gh", "release", "view", tag], check=False, capture_output=True, text=True)
    return result.returncode == 0


def digests_from(invocation: Invocation) -> dict[str, str]:
    return {path: output.digest for path, output in invocation.by_path().items()}


def plan(
    releases: list[Release], digests: Mapping[str, str], tag_exists: Callable[[str], bool], workers: int
) -> list[tuple[Release, bool]]:
    """Pair every release with whether it is already published. Tag lookups dominate."""
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        published = pool.map(lambda r: is_published(r, digests, tag_exists), releases)
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
    parser.add_argument(
        "--invocations",
        default="",
        help="comma-separated bazel-ci invocation ids; empty releases everything (fail open)",
    )
    parser.add_argument("--workers", type=int, default=8, help="concurrent tag lookups")
    parser.add_argument(
        "--commit-subject",
        default="",
        help="subject of the commit being released; a [skip ci] subject releases nothing",
    )

    args = parser.parse_args(argv)
    releases = load_releases(args.artifact_targets, args.skills_registry)

    if "[skip ci]" in args.commit_subject:
        print("Commit is marked [skip ci]; releasing nothing.", file=sys.stderr)
        _write_github_output(matrix=json.dumps({"include": []}), count="0")
        return 0

    # bazel-ci runs `bazel test` then `bazel build`, so it reports more than one
    # invocation; merge them rather than guess which one holds a given output.
    invocations = [i for i in args.invocations.split(",") if i]
    digests: dict[str, str] = {}
    if not invocations:
        print("::warning::no bazel-ci invocation to read; releasing everything", file=sys.stderr)
    for invocation in invocations:
        try:
            digests.update(digests_from(read(invocation)))
        except BuildBuddyError as e:
            print(
                f"::warning::could not read invocation {invocation} ({e}); releasing more than needed", file=sys.stderr
            )

    decided = plan(releases, digests, gh_tag_exists, args.workers)
    include = matrix_include(decided)

    for release, published in sorted(decided, key=lambda d: d[0].pkg):
        print(f"{'skip' if published else 'RELEASE':>7}  {release.pkg}", file=sys.stderr)
    print(f"\n{len(include)} of {len(decided)} releases need publishing.", file=sys.stderr)

    _write_github_output(matrix=json.dumps({"include": include}), count=str(len(include)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
