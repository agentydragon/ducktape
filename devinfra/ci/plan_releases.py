"""Decide which releases actually need publishing, before the matrix fans out.

The planner avoids a per-artifact runner job when the content is unchanged.

This runs once in the matrix job instead, and builds nothing at all. For targets
reported by `bazel-ci`, BuildBuddy still holds that invocation's build event
stream — including each output's content digest. The planner reads those digests,
turns them into the tags they would be published under, and asks which of those
tags already exist. A target not covered by the sweep stays in the matrix.

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
"""

from __future__ import annotations

import argparse
import concurrent.futures
import subprocess
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from urllib.parse import quote

from devinfra.buildbuddy.ci.bes import Output
from devinfra.ci import publication_cli, publication_evidence
from devinfra.ci.release_assets import Release, load_releases, resolve_asset_output
from devinfra.ci.release_content_hash import release_content_hash_from_digests

TAG_HASH_LENGTH = 12

REPO = "agentydragon/ducktape"


def content_tag(pkg: str, content_hash: str) -> str:
    return f"{pkg}-{content_hash[:TAG_HASH_LENGTH]}"


def content_hash(release: Release, by_label: Mapping[str, list[Output]]) -> str | None:
    """This release's published identity, or None if the build did not report it.

    A target absent from the stream is the expected case: bazel-ci builds `//...`,
    which reaches neither `@ducktape_activitywatch//...` (aw-importer) nor `manual`
    targets (gterm-theme), so those always take the slow path. A target that *is*
    in the stream but resolves to no single artifact, or to a file whose name
    contradicts the pin's `filename`, raises instead — that is SSOT drift worth a
    warning, and the row's own job then fails loudly on the same check.
    """
    assets = []
    for target, filename in release.assets:
        if target not in by_label:
            return None
        artifact = resolve_asset_output(by_label, target, filename)
        assets.append((filename, artifact.digest))
    return release_content_hash_from_digests(assets)


def is_published(release: Release, by_label: Mapping[str, list[Output]], tag_exists: Callable[[str], bool]) -> bool:
    """True only when this release's exact content is already published.

    Every uncertain path returns False so the row stays in the matrix.
    """
    if not release.uses_default_config:
        return False
    try:
        digest = content_hash(release, by_label)
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
    """Whether a GitHub release exists for `tag`, using REST rather than GraphQL."""
    result = subprocess.run(
        ["gh", "api", f"repos/{REPO}/releases/tags/{quote(tag, safe='')}"], check=False, capture_output=True, text=True
    )
    return result.returncode == 0


def plan(
    releases: list[Release], by_label: Mapping[str, list[Output]], tag_exists: Callable[[str], bool], workers: int
) -> list[tuple[Release, bool]]:
    """Pair every release with whether it is already published. Tag lookups dominate."""
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        published = pool.map(lambda r: is_published(r, by_label, tag_exists), releases)
    return list(zip(releases, published, strict=True))


def matrix_include(
    decided: list[tuple[Release, bool]], reusable_tests: set[str] | None = None, blocked_tests: set[str] | None = None
) -> list[dict[str, str]]:
    """Matrix rows for releases; one whose tests push CI already failed never starts a job."""
    reusable_tests = reusable_tests or set()
    blocked_tests = blocked_tests or set()
    rows = []
    for release, published in decided:
        if published or (release.tests and release.tests in blocked_tests):
            continue
        row = release.as_matrix_row()
        if release.uses_default_config and release.tests and release.tests in reusable_tests:
            row["reuse_test_verdict"] = "true"
        rows.append(row)
    return rows


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact-targets", type=Path, default=Path("devinfra/ci/artifact_targets.json"))
    parser.add_argument("--skills-registry", type=Path, default=Path("skills/skills_registry.json"))
    publication_cli.add_evidence_options(parser)
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
        publication_cli.write_matrix([])
        return

    evidence = publication_cli.load_evidence(args)
    by_label = evidence.outputs.by_label() if evidence.outputs is not None else {}

    decided = plan(releases, by_label, gh_tag_exists, args.workers)
    reusable_tests, blocked_tests = evidence.test_gates(
        (
            publication_evidence.TestGateCandidate(
                release.pkg, release.tests, uses_default_config=release.uses_default_config
            )
            for release, published in decided
            if not published
        ),
        action="released",
    )
    include = matrix_include(decided, reusable_tests, blocked_tests)

    for release, published in sorted(decided, key=lambda d: d[0].pkg):
        skipped = published or (release.tests and release.tests in blocked_tests)
        print(f"{'skip' if skipped else 'RELEASE':>7}  {release.pkg}", file=sys.stderr)
    print(f"\n{len(include)} of {len(decided)} releases need publishing.", file=sys.stderr)

    publication_cli.write_matrix(include)


if __name__ == "__main__":
    main()
