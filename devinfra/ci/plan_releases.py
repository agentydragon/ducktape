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

Publication jobs may reuse verdicts for literal workspace test labels only when
the same successful push-CI invocation proves source, configuration and one
passing attempt. Suites, patterns and custom-flag rows keep their test gates.
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
from urllib.parse import quote

from devinfra.ci.bes import BuildBuddyError, Invocation, Output, artifact_output, merge, read
from devinfra.ci.publication_test_verdicts import (
    effective_rbe_image,
    recorded_test_failures,
    selector_already_failed,
    selector_is_covered,
    verified_test_summaries,
)
from devinfra.ci.release_content_hash import release_content_hash_from_digests

TAG_HASH_LENGTH = 12

REPO = "agentydragon/ducktape"


@dataclasses.dataclass(frozen=True)
class Release:
    """One row of the release matrix.

    Field names are the matrix keys release.yml reads — except `filenames`,
    which only the planner's own identity derivation consumes.
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
        artifact = artifact_output(by_label, target)
        if isinstance(artifact, str):
            if target not in by_label:
                return None
            raise ValueError(artifact)
        if (built := Path(artifact.path).name) != filename:
            raise ValueError(f"{target} built {built!r} but its pin says filename={filename!r}")
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


def _write_github_output(**values: str) -> None:
    if not (path := os.environ.get("GITHUB_OUTPUT")):
        return
    with Path(path).open("a") as f:
        f.writelines(f"{key}={value}\n" for key, value in values.items())


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact-targets", type=Path, default=Path("devinfra/ci/artifact_targets.json"))
    parser.add_argument("--skills-registry", type=Path, default=Path("skills/skills_registry.json"))
    parser.add_argument(
        "--invocations",
        default="",
        help="comma-separated bazel-ci invocation ids; empty releases everything (fail open)",
    )
    parser.add_argument("--workers", type=int, default=8, help="concurrent tag lookups")
    parser.add_argument("--test-invocation-id", default="", help="exact bazel-ci test invocation id")
    parser.add_argument("--test-source-commit", default="", help="source SHA verified by bazel-ci checkout")
    parser.add_argument("--rbe-container-image", default="", help="publication test RBE image override, if any")
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
        return

    # bazel-ci runs `bazel test` then `bazel build`, so it reports more than one
    # invocation; merge them rather than guess which one holds a given output.
    invocation_ids = [i for i in args.invocations.split(",") if i]
    if not invocation_ids:
        print("::warning::no bazel-ci invocation to read; releasing everything", file=sys.stderr)
    readable: dict[str, Invocation] = {}
    for invocation_id in invocation_ids:
        try:
            readable[invocation_id] = read(invocation_id)
        except (BuildBuddyError, KeyError, TypeError, ValueError, IndexError) as e:
            print(
                f"::warning::could not read invocation {invocation_id} ({e}); releasing more than needed",
                file=sys.stderr,
            )
    merged = merge(readable.values())
    by_label = merged.by_label() if merged is not None else {}
    test_invocation = readable.get(args.test_invocation_id) if args.test_invocation_id in invocation_ids else None
    summaries, evidence_problem = verified_test_summaries(
        test_invocation,
        invocation_id_value=args.test_invocation_id,
        source_commit=args.test_source_commit,
        current_commit=os.environ.get("GITHUB_SHA", ""),
        run_id=os.environ.get("GITHUB_RUN_ID", ""),
        run_attempt=os.environ.get("GITHUB_RUN_ATTEMPT", ""),
        event_name=os.environ.get("GITHUB_EVENT_NAME", ""),
        rbe_image=effective_rbe_image(args.rbe_container_image),
    )

    failed_labels = recorded_test_failures(
        test_invocation,
        invocation_id_value=args.test_invocation_id,
        source_commit=args.test_source_commit,
        current_commit=os.environ.get("GITHUB_SHA", ""),
        run_id=os.environ.get("GITHUB_RUN_ID", ""),
        run_attempt=os.environ.get("GITHUB_RUN_ATTEMPT", ""),
        event_name=os.environ.get("GITHUB_EVENT_NAME", ""),
        rbe_image=effective_rbe_image(args.rbe_container_image),
    )

    decided = plan(releases, by_label, gh_tag_exists, args.workers)
    reusable_tests = set()
    blocked_tests = set()
    for release, published in decided:
        if published or not release.tests:
            continue
        blocked, why = selector_already_failed(release.tests, failed_labels)
        if blocked:
            blocked_tests.add(release.tests)
            print(f"::warning::{release.pkg}: not released, {why}", file=sys.stderr)
            continue
        if not release.uses_default_config:
            continue
        covered, reason = (
            selector_is_covered(release.tests, summaries) if evidence_problem is None else (False, evidence_problem)
        )
        if covered:
            reusable_tests.add(release.tests)
            print(f"::notice::{release.pkg}: reusing the verified bazel-ci test verdict", file=sys.stderr)
        else:
            print(f"::notice::{release.pkg}: retaining its test gate ({reason})", file=sys.stderr)
    include = matrix_include(decided, reusable_tests, blocked_tests)

    for release, published in sorted(decided, key=lambda d: d[0].pkg):
        skipped = published or (release.tests and release.tests in blocked_tests)
        print(f"{'skip' if skipped else 'RELEASE':>7}  {release.pkg}", file=sys.stderr)
    print(f"\n{len(include)} of {len(decided)} releases need publishing.", file=sys.stderr)

    _write_github_output(matrix=json.dumps({"include": include}), count=str(len(include)))


if __name__ == "__main__":
    main()
