"""Publish immutable visual-review bundles from Bazel tests run by trusted CI."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import mimetypes
import os
import re
import shutil
import subprocess
import tempfile
import time
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from threading import Event
from typing import Any, Literal, Protocol, cast

import boto3
from botocore.exceptions import ClientError
from PIL import Image

from devinfra.buildbuddy.ci.invocation_ids import invocation_id
from devinfra.pr_visuals.artifacts import Runner
from devinfra.pr_visuals.check_run import upsert_check_run
from devinfra.pr_visuals.collection import CollectionIssue, OutputObservation, ResultKey, collect_outputs
from devinfra.pr_visuals.comment_publishing import CommentUpdate, publish_pull_request_comment
from devinfra.pr_visuals.gallery import review_test_page_data, templates
from devinfra.pr_visuals.github_comment import (
    CommentState,
    ci_failure_summary,
    error_comment_body,
    no_visual_comment_body,
    success_comment_body,
)
from devinfra.pr_visuals.metadata import (
    BaselineMetadata,
    BaselinePointer,
    ClassificationCounts,
    ReviewAsset,
    ReviewBundleMetadata,
    ReviewIssue,
    ReviewIssueSource,
    ReviewResult,
    ReviewTest,
    ReviewVariant,
)
from devinfra.pr_visuals.pull_request import find_reviewable_pull_request
from devinfra.pr_visuals.render_common import classification_totals, ordered_assets, shared_image_key, test_impact_key
from devinfra.pr_visuals.visual_diff import compare_pngs

logger = logging.getLogger(__name__)

FULL_SHA = re.compile(r"^[0-9a-f]{40}$")
REVIEW_PREFIX = "reviews-test-outputs"


@dataclass(frozen=True)
class _Download:
    uri: str
    invocation: str
    destination: Path


@dataclass(frozen=True)
class DownloadedVariant:
    path: Path
    sha256: str
    results: list[ReviewResult]


@dataclass(frozen=True)
class DownloadedAsset:
    case_id: str
    checkpoint: str | None
    role: str
    label: str
    variants: list[DownloadedVariant]

    @property
    def path(self) -> str:
        identity = json.dumps([self.case_id, self.checkpoint, self.role], ensure_ascii=False, separators=(",", ":"))
        return f"{hashlib.sha256(identity.encode()).hexdigest()}.png"


@dataclass(frozen=True)
class DownloadedVisualTest:
    target_label: str
    slug: str
    title: str
    assets: list[DownloadedAsset]
    configuration: str | None
    issues: list[ReviewIssue]
    notices: list[ReviewIssue] = field(default_factory=list)


def _group_result_messages(results: Iterable[tuple[ResultKey, str]]) -> list[ReviewIssue]:
    groups: dict[tuple[str, str | None, int | None, int | None, str], set[int | None]] = defaultdict(set)
    for result, message in results:
        groups[result.invocation, result.configuration, result.run, result.attempt, message].add(result.shard)
    return [
        ReviewIssue(
            message=message,
            source=ReviewIssueSource(
                invocation=invocation,
                configuration=configuration,
                run=run,
                attempt=attempt,
                shards=sorted(shards, key=lambda shard: (shard is None, shard or 0)),
            ),
        )
        for (invocation, configuration, run, attempt, message), shards in groups.items()
    ]


def _collection_issue_message(issue: CollectionIssue) -> str:
    details: list[str] = []
    if issue.output is not None:
        checkpoint = "<default>" if issue.output.checkpoint is None else issue.output.checkpoint
        details.extend((f"case {issue.output.case_id!r}", f"checkpoint {checkpoint!r}", f"role {issue.output.role!r}"))
    if issue.artifact is not None:
        details.append(f"artifact {issue.artifact!r}")
    if issue.record is not None:
        details.append(f"record {issue.record!r}")
    return issue.message if not details else f"{issue.message}: {', '.join(details)}"


def _group_collection_issues(issues: Iterable[CollectionIssue]) -> list[ReviewIssue]:
    def result_messages() -> Iterable[tuple[ResultKey, str]]:
        for issue in issues:
            if issue.result is None:
                raise ValueError("per-result collection issue has no result provenance")
            message = _collection_issue_message(issue)
            yield from ((result, message) for result in (issue.result, *issue.related_results))

    return _group_result_messages(result_messages())


class BaselineSource(Protocol):
    """Opaque reader over the published visual-review object store.

    The publisher owns key construction (commit + target + asset); the source is
    just ``key → bytes`` (``None`` when the object is absent), so a fake in tests
    is a plain ``dict[str, bytes]``.
    """

    def fetch(self, key: str) -> bytes | None: ...


def _is_not_found(error: ClientError) -> bool:
    return error.response.get("Error", {}).get("Code") in ("NoSuchKey", "404")


@dataclass(frozen=True)
class S3BaselineSource:
    client: Any
    bucket: str

    def fetch(self, key: str) -> bytes | None:
        try:
            body = self.client.get_object(Bucket=self.bucket, Key=key)["Body"].read()
        except ClientError as error:
            if _is_not_found(error):
                return None
            raise
        return cast(bytes, body)


def _baseline_pointer_key(slug: str) -> str:
    """Mutable per-target pointer to the last devel commit that published it.

    A PR's base commit bundle may lack targets that its CI run did not cover.
    The pointer names the newest devel commit whose immutable review bundle
    carries the target, and PR runs use it when the base bundle misses.
    """
    return f"baselines/{REVIEW_PREFIX}/{slug}.json"


@dataclass(frozen=True)
class ResolvedBaseline:
    commit_sha: str
    metadata: BaselineMetadata
    fallback: bool


def _review_key(commit_sha: str, slug: str, *parts: str) -> str:
    return "/".join((REVIEW_PREFIX, commit_sha, "tests", slug, *parts))


def _resolve_baseline(slug: str, base_sha: str | None, source: BaselineSource) -> ResolvedBaseline | None:
    def metadata_at(sha: str) -> BaselineMetadata | None:
        body = source.fetch(_review_key(sha, slug, "metadata.json"))
        if body is None:
            return None
        metadata = BaselineMetadata.model_validate_json(body)
        return metadata if metadata.baseline_eligible else None

    if base_sha is not None and (metadata := metadata_at(base_sha)) is not None:
        return ResolvedBaseline(commit_sha=base_sha, metadata=metadata, fallback=False)
    if (body := source.fetch(_baseline_pointer_key(slug))) is not None:
        pointer = BaselinePointer.model_validate_json(body)
        if (metadata := metadata_at(pointer.commit_sha)) is not None:
            return ResolvedBaseline(commit_sha=pointer.commit_sha, metadata=metadata, fallback=True)
    return None


INVOCATION_WAIT_SECONDS = 300
INVOCATION_POLL_SECONDS = 2
S3_IMAGE_UPLOAD_WORKERS = 8


def search_ci_test_invocations(
    commit_sha: str, *, repository: str, bbapi: Path = Path("bbapi"), run: Runner = subprocess.run
) -> list[str]:
    """The CI test invocations BuildBuddy holds for `commit_sha`, full sweep first.

    A commit has several CI runs with different target sets — a `//...` devel sweep
    alongside affected-set runs, and the sweep can cover more visual manifests.
    """
    result = run(
        [
            bbapi,
            "--timeout",
            "30s",
            "invocation",
            "list",
            "--repo",
            f"https://github.com/{repository}",
            "--commit",
            commit_sha,
            "--command",
            "test",
            "--role",
            "CI",
            "--count",
            "25",
            "--json",
        ],
        check=False,
        text=True,
        capture_output=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"bbapi invocation list failed: {result.stderr.strip()}")
    found = json.loads(result.stdout).get("invocation", [])
    sweeps = [i["invocationId"] for i in found if i.get("pattern") == ["//..."]]
    return sweeps or [i["invocationId"] for i in found]


def find_test_invocations(
    *,
    run_id: str,
    run_attempt: str,
    commit_sha: str,
    repository: str,
    bbapi: Path = Path("bbapi"),
    run: Runner = subprocess.run,
) -> list[str]:
    """Where this commit's test artifacts are, by commit if BuildBuddy knows, else by run.

    The by-run derivation (devinfra/buildbuddy/ci/invocation_ids.py) is the only
    handle on a PR run, which records the merge SHA
    and so cannot be found by head SHA. It is the fallback rather than the primary
    because it names the run that triggered this publish, which is frequently not the
    run that ran the tests.

    The two are not combined: `download_visual_tests` rejects a target whose rosters
    disagree across invocations, and mixing a sweep with an affected-set run invites
    exactly that.
    """
    if found := search_ci_test_invocations(commit_sha, repository=repository, bbapi=bbapi, run=run):
        return found
    return [str(invocation_id(run_id=run_id, attempt=run_attempt, role=role)) for role in ("test", "build")]


def wait_for_test_invocations(
    invocations: list[str],
    *,
    bbapi: Path = Path("bbapi"),
    run: Runner = subprocess.run,
    workflow_succeeded: bool = False,
    allow_build_only: bool = False,
    build_invocation_id: str | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    wait: Callable[[float], object] | None = None,
) -> Literal["complete", "absent", "incomplete"]:
    """Wait for BuildBuddy to finish streaming known invocations before listing artifacts.

    A successful workflow can have no Bazel invocation when no targets were
    affected. A completed build with no test invocation proves Bazel selected no
    tests. A failed workflow alone cannot prove an absent remote
    invocation will not appear later.
    """

    def status(invocation: str) -> str | None:
        result = run(
            [bbapi, "--timeout", "30s", "invocation", "status", invocation, "--json"],
            check=False,
            text=True,
            capture_output=True,
        )
        if result.returncode != 0:
            raise RuntimeError(f"bbapi invocation status for {invocation} failed: {result.stderr.strip()}")
        found = json.loads(result.stdout).get("invocation") or []
        return found[0].get("invocationStatus") if found else None

    wait = wait or Event().wait
    deadline = monotonic() + INVOCATION_WAIT_SECONDS
    while True:
        observed = [status(invocation) for invocation in invocations]
        if all(item == "COMPLETE_INVOCATION_STATUS" for item in observed):
            return "complete"
        if all(item is None for item in observed) and (workflow_succeeded or allow_build_only):
            # bazel_ci.py skips `bazel test` when no tests are selected, then runs
            # the build. It can also skip both commands when nothing is affected.
            build_status = status(build_invocation_id) if build_invocation_id else None
            if (allow_build_only and build_status == "COMPLETE_INVOCATION_STATUS") or (
                workflow_succeeded and build_status is None
            ):
                return "absent"
        remaining = deadline - monotonic()
        if remaining <= 0:
            return "incomplete"
        wait(min(INVOCATION_POLL_SECONDS, remaining))


def list_ci_failures(invocations: list[str], *, bbapi: Path = Path("bbapi"), run: Runner = subprocess.run) -> list[str]:
    """Return failed Bazel target labels, best-effort, from linked invocations.

    A failed ``bazel test`` still uploads the artifacts for targets that completed
    before the failure.  BuildBuddy's target summaries are the durable source for
    identifying the targets that did not complete successfully; individual linked
    invocations may disappear before the publisher gets to them, so an unavailable
    query is intentionally not fatal to visual publication.
    """
    failures: set[str] = set()
    for invocation in invocations:
        result = run([bbapi, "target", invocation, "--json"], check=False, text=True, capture_output=True)
        if result.returncode != 0:
            continue
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        for group in payload.get("targetGroups") or []:
            if not isinstance(group, dict):
                continue
            for target in group.get("targets") or []:
                if not isinstance(target, dict):
                    continue
                if target.get("status") == "FAILED":
                    metadata = target.get("metadata")
                    label = metadata.get("label") if isinstance(metadata, dict) else None
                    if isinstance(label, str):
                        failures.add(label)
    return sorted(failures)


def target_slug(label: str, *, repository: str) -> str:
    readable = re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")
    if not readable:
        raise ValueError(f"cannot create URL slug for Bazel target {label!r}")
    digest = hashlib.sha256(json.dumps([repository, label], separators=(",", ":")).encode()).hexdigest()[:16]
    return f"{readable[:80]}-{digest}"


def _download_all(downloads: list[_Download], *, bbapi: Path, run: Runner) -> None:
    """Download a batch of listed CAS blobs in one bbapi operation.

    The manifest carries the URI BuildBuddy advertised and its exact destination, so the
    CLI can download concurrently without resolving the invocation's event stream once
    per file.
    """
    if not downloads:
        return
    for download in downloads:
        download.destination.parent.mkdir(parents=True, exist_ok=True)
    manifest = [
        {"uri": download.uri, "destination": str(download.destination), "invocation_id": download.invocation}
        for download in downloads
    ]
    manifest_dir = downloads[0].destination.parent
    manifest_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=manifest_dir, delete=False) as stream:
        json.dump(manifest, stream)
        manifest_path = Path(stream.name)
    try:
        result = run(
            [bbapi, "--timeout", "30s", "blob", "download", "--manifest", str(manifest_path)],
            check=False,
            text=True,
            capture_output=True,
        )
        if result.returncode != 0:
            raise RuntimeError(f"bbapi blob download failed: {result.stderr.strip()}")
    finally:
        manifest_path.unlink(missing_ok=True)
    missing = [str(download.destination) for download in downloads if not download.destination.is_file()]
    if missing:
        raise RuntimeError(f"bbapi blob download did not create destinations: {', '.join(missing)}")


def download_visual_tests(
    invocations: list[str],
    destination: Path,
    *,
    repository: str,
    bbapi: Path = Path("bbapi"),
    run: Runner = subprocess.run,
) -> list[DownloadedVisualTest]:
    """Materialize screenshot variants selected from exact test results by the collector."""
    collected = collect_outputs(invocations, repository, destination / ".records", bbapi=bbapi, run=run)
    by_target: dict[str, list[OutputObservation]] = defaultdict(list)
    for observation in collected.selected:
        if observation.key.role == "screenshot":
            by_target[observation.key.target].append(observation)
    # A failed attempt can be the only evidence that this target produces screenshots.
    # Expose an incomplete target rather than silently reporting no visual outputs.
    for observation in collected.observations:
        if observation.key.role == "screenshot":
            by_target.setdefault(observation.key.target, [])
    for result in collected.results:
        if result.record_count and (result.issues or result.retried_by is not None):
            by_target.setdefault(result.result.target, [])
    tests: list[DownloadedVisualTest] = []
    downloads: list[_Download] = []
    for target, selected in sorted(by_target.items()):
        target_results = [entry for entry in collected.results if entry.result.target == target]
        issues = _group_collection_issues(issue for entry in target_results for issue in entry.issues)
        if any(entry.overall_status not in ("PASSED", "FLAKY") for entry in target_results):
            issues.append(ReviewIssue(message="The overall test result did not complete successfully."))
        notices = _group_result_messages(
            (entry.result, f"superseded by attempt {entry.retried_by}")
            for entry in target_results
            if entry.retried_by is not None
        )
        configurations = {entry.result.configuration for entry in target_results}
        configuration = next(iter(configurations)) if len(configurations) == 1 else None
        if len(configurations) != 1 or None in configurations:
            issues.append(
                ReviewIssue(message="Multiple or unknown Bazel configurations prevent a coherent comparison.")
            )
        run_rosters: dict[tuple[str, str | None, int | None], set[tuple[str, str | None, str]]] = {}
        for result in target_results:
            if result.selected:
                run_key = (result.result.invocation, result.result.configuration, result.result.run)
                run_rosters.setdefault(run_key, set())
        grouped: dict[tuple[str, str | None, str], list[OutputObservation]] = defaultdict(list)
        for observation in selected:
            identity = (observation.key.case_id, observation.key.checkpoint, observation.key.role)
            grouped[identity].append(observation)
            run_rosters[(observation.result.invocation, observation.result.configuration, observation.result.run)].add(
                identity
            )
        if len({frozenset(roster) for roster in run_rosters.values()}) > 1:
            issues.append(ReviewIssue(message="Successful runs or invocations published different screenshot rosters."))
        titles = {item.title for item in selected if item.title}
        if len(titles) > 1:
            issues.append(ReviewIssue(message="Screenshot records disagree on the target title."))
        title = min(titles) if titles else target
        assets: list[DownloadedAsset] = []
        for (case_id, checkpoint, role), observations in sorted(grouped.items(), key=lambda pair: repr(pair[0])):
            labels = {item.label for item in observations if item.label}
            if len(labels) > 1:
                issues.append(ReviewIssue(message=f"{case_id}: screenshot records disagree on the display label."))
            label = min(labels) if labels else case_id + (f" — {checkpoint}" if checkpoint else "")
            variants_by_digest: dict[str, list[OutputObservation]] = defaultdict(list)
            for item in observations:
                if item.digest is None:
                    issues.append(ReviewIssue(message=f"{case_id}: the screenshot has no verified CAS digest."))
                    continue
                variants_by_digest[item.digest].append(item)
            variants: list[DownloadedVariant] = []
            for digest, items in sorted(variants_by_digest.items()):
                item = items[0]
                image_path = destination / target_slug(target, repository=repository) / f"{digest}.png"
                downloads.append(_Download(item.uri, item.result.invocation, image_path))
                variants.append(
                    DownloadedVariant(
                        image_path,
                        digest,
                        [
                            ReviewResult(
                                invocation=source.result.invocation,
                                configuration=source.result.configuration,
                                run=source.result.run,
                                shard=source.result.shard,
                                attempt=source.result.attempt,
                                status=source.status,
                                uri=source.uri,
                                record_uri=source.record_uri,
                            )
                            for source in items
                        ],
                    )
                )
            if variants:
                assets.append(DownloadedAsset(case_id, checkpoint, role, label, variants))
        tests.append(
            DownloadedVisualTest(
                target, target_slug(target, repository=repository), title, assets, configuration, issues, notices
            )
        )
    # Multiple cases may deliberately have identical bytes. Download each
    # target/digest destination once; concurrent writers to a path are unsafe.
    unique_downloads = {download.destination: download for download in downloads}
    logger.info("Downloading %d screenshot blobs from %d targets", len(unique_downloads), len(tests))
    _download_all(list(unique_downloads.values()), bbapi=bbapi, run=run)
    return tests


def _asset_summary(assets: list[ReviewAsset]) -> ClassificationCounts:
    counts: Counter[str] = Counter()
    for asset in assets:
        if asset.classification is not None:
            counts[asset.classification] += 1
    return ClassificationCounts(**counts)


def _pick_preview(assets: list[ReviewAsset]) -> ReviewAsset | None:
    """The one asset shown as a test's thumbnail on the aggregate index page.

    Prefers a `modified` asset (there's a diff to draw attention to); falls back to a `new` one
    (a target whose assets are all new — e.g. its first publish, or a PR that only adds fixtures
    to an existing target — otherwise gets no thumbnail at all).
    """
    ordered = ordered_assets(assets)
    return next(
        (asset for asset in ordered if asset.classification == "unstable"),
        next(
            (asset for asset in ordered if asset.classification == "modified"),
            next((asset for asset in ordered if asset.classification == "new"), None),
        ),
    )


def _asset_identity(asset: DownloadedAsset | ReviewAsset) -> tuple[str, str | None, str]:
    return asset.case_id, asset.checkpoint, asset.role


def _review_asset(
    asset: DownloadedAsset, classification: Literal["unchanged", "modified", "new", "removed", "unstable"] | None = None
) -> ReviewAsset:
    return ReviewAsset(
        path=asset.path,
        label=asset.label,
        case_id=asset.case_id,
        checkpoint=asset.checkpoint,
        role=asset.role,
        variants=[ReviewVariant(sha256=v.sha256, results=v.results) for v in asset.variants],
        classification=classification,
    )


def _classify_test_assets(
    test: DownloadedVisualTest, baseline: ResolvedBaseline, baseline_source: BaselineSource, target_dir: Path
) -> list[ReviewAsset]:
    """Compare structured identities; paths only address the rendered page and staging files."""
    baseline_by_identity = {_asset_identity(asset): asset for asset in baseline.metadata.assets}
    if len(baseline_by_identity) != len(baseline.metadata.assets):
        raise ValueError(f"{test.target_label}: duplicate identities in baseline metadata")
    baseline_dir = target_dir / "baseline"
    diff_dir = target_dir / "diff"
    enriched: list[ReviewAsset] = []
    stale = True if baseline.fallback else None

    def materialize_baseline(asset: ReviewAsset, path: str) -> Path:
        if asset.candidate_sha256 is None:
            raise ValueError(f"{test.target_label}: baseline has no image digest for {asset.case_id!r}")
        body = baseline_source.fetch(shared_image_key(asset.candidate_sha256))
        if body is None:
            raise ValueError(f"{test.target_label}: missing baseline image {asset.candidate_sha256}")
        if hashlib.sha256(body).hexdigest() != asset.candidate_sha256:
            raise ValueError(f"{test.target_label}: corrupted baseline image {asset.candidate_sha256}")
        baseline_dir.mkdir(parents=True, exist_ok=True)
        destination = baseline_dir / path
        destination.write_bytes(body)
        return destination

    for asset in test.assets:
        if len(asset.variants) > 1:
            enriched.append(_review_asset(asset, "unstable"))
            continue
        baseline_asset = baseline_by_identity.get(_asset_identity(asset))
        if baseline_asset is None:
            enriched.append(_review_asset(asset, "new"))
            continue
        candidate = asset.variants[0]
        if candidate.sha256 == baseline_asset.candidate_sha256:
            enriched.append(_review_asset(asset, "unchanged"))
            continue
        baseline_png = materialize_baseline(baseline_asset, asset.path)
        comparison = compare_pngs(candidate.path, baseline_png)
        if comparison.classification == "unchanged":
            baseline_png.unlink()
            enriched.append(_review_asset(asset, "unchanged"))
            continue
        if comparison.diff_overlay is not None:
            diff_dir.mkdir(parents=True, exist_ok=True)
            comparison.diff_overlay.save(diff_dir / asset.path)
        result = _review_asset(asset, "modified")
        result.changed_fraction = comparison.changed_fraction
        result.changed_pixels = comparison.changed_pixels
        result.candidate_dimensions = list(comparison.actual_size)
        result.baseline_dimensions = list(comparison.baseline_size)
        result.dimension_changed = comparison.dimension_changed
        result.baseline_stale = stale
        enriched.append(result)
    candidate_identities = {_asset_identity(asset) for asset in test.assets}
    # An incomplete run cannot prove that any baseline screenshot was removed.
    if not test.issues:
        for identity, baseline_asset in baseline_by_identity.items():
            if identity in candidate_identities:
                continue
            materialize_baseline(baseline_asset, baseline_asset.path)
            removed = baseline_asset.model_copy(deep=True)
            removed.classification = "removed"
            removed.candidate_sha256 = None
            removed.variants = []
            removed.baseline_stale = stale
            enriched.append(removed)
    return enriched


def _share_png(image: Path, output_root: Path, *, known_digest: str | None = None) -> str | None:
    if not image.is_file():
        return None
    digest = known_digest or hashlib.sha256(image.read_bytes()).hexdigest()
    destination = output_root / shared_image_key(digest)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        if hashlib.sha256(destination.read_bytes()).hexdigest() != digest:
            raise ValueError(f"corrupt staged image at {destination}")
        image.unlink()
    else:
        image.replace(destination)
    return digest


def build_bundle(
    tests: list[DownloadedVisualTest],
    output_root: Path,
    *,
    commit_sha: str,
    repository: str,
    base_sha: str | None = None,
    baseline_source: BaselineSource | None = None,
) -> Path:
    if not FULL_SHA.fullmatch(commit_sha):
        raise ValueError("commit SHA must be the full 40-character lowercase SHA-1")
    if not tests:
        raise ValueError("cannot build a visual-review bundle without tests")

    bundle = output_root / REVIEW_PREFIX / commit_sha
    environment = templates()
    bundle.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(Path(__file__).with_name("gallery.css"), bundle / "gallery.css")
    shutil.copyfile(Path(__file__).with_name("gallery_sort.js"), bundle / "gallery_sort.js")
    shutil.copyfile(Path(__file__).with_name("gallery_slider.css"), bundle / "gallery_slider.css")
    shutil.copyfile(Path(__file__).with_name("gallery_slider.js"), bundle / "gallery_slider.js")
    review_tests: list[ReviewTest] = []
    for test in tests:
        target_dir = bundle / "tests" / test.slug
        target_dir.mkdir(parents=True, exist_ok=True)
        issues = list(test.issues)
        if not test.configuration:
            issues.append(
                ReviewIssue(
                    message="The Bazel configuration is unknown; comparison and baseline publication are unavailable."
                )
            )
        baseline: ResolvedBaseline | None = None
        comparable = bool(test.configuration)
        if base_sha and baseline_source is not None:
            baseline = _resolve_baseline(test.slug, base_sha, baseline_source)
            if baseline is not None and (
                baseline.metadata.comparison_profile != "bazel-configuration-exact-v1"
                or not test.configuration
                or baseline.metadata.configuration != test.configuration
            ):
                issues.append(
                    ReviewIssue(
                        message="The baseline and candidate configurations cannot be compared under bazel-configuration-exact-v1."
                    )
                )
                comparable = False
                assets = [
                    _review_asset(asset, "unstable" if len(asset.variants) > 1 else None) for asset in test.assets
                ]
            elif baseline is None:
                assets = [
                    _review_asset(asset, "unstable" if len(asset.variants) > 1 else "new" if comparable else None)
                    for asset in test.assets
                ]
            else:
                assets = _classify_test_assets(test, baseline, baseline_source, target_dir)
        else:
            assets = [_review_asset(asset, "unstable" if len(asset.variants) > 1 else None) for asset in test.assets]
        for asset in test.assets:
            for index, variant in enumerate(asset.variants):
                body = variant.path.read_bytes()
                if hashlib.sha256(body).hexdigest() != variant.sha256:
                    raise ValueError(f"{test.target_label}: screenshot content digest mismatch for {asset.case_id!r}")
                with Image.open(variant.path) as image:
                    if image.format != "PNG":
                        raise ValueError(f"{test.target_label}: screenshot is not PNG: {asset.case_id!r}")
                    image.verify()
                staged = target_dir / f".{asset.path}.{index}"
                staged.write_bytes(body)
                _share_png(staged, output_root, known_digest=variant.sha256)
        for review_asset in assets:
            if len(review_asset.variants) == 1:
                review_asset.candidate_sha256 = review_asset.variants[0].sha256
            review_asset.baseline_sha256 = _share_png(target_dir / "baseline" / review_asset.path, output_root)
            review_asset.diff_sha256 = _share_png(target_dir / "diff" / review_asset.path, output_root)
        review_test = ReviewTest(
            target_label=test.target_label,
            slug=test.slug,
            title=test.title,
            assets=assets,
            issues=issues,
            notices=test.notices,
            configuration=test.configuration,
            baseline_eligible=bool(test.configuration)
            and bool(test.assets)
            and not issues
            and all(len(a.variants) == 1 for a in test.assets),
            base_sha=(baseline.commit_sha if baseline is not None else base_sha) if comparable else None,
            merge_base_sha=base_sha,
            baseline_fallback=baseline.fallback if baseline is not None else None,
            summary=_asset_summary(assets) if base_sha else None,
            preview=_pick_preview(assets) if base_sha else None,
        )
        (target_dir / "metadata.json").write_text(review_test.model_dump_json(indent=2, exclude_none=True) + "\n")
        page = review_test_page_data(review_test, asset_base="", page_url="", shared_asset_base="../../../../")
        (target_dir / "index.html").write_text(
            environment.get_template("pr_visual_test.html.j2").render(
                repository=repository,
                commit_sha=commit_sha,
                gallery_stylesheet="../../gallery.css",
                gallery_slider_stylesheet="../../gallery_slider.css",
                gallery_slider_script="../../gallery_slider.js",
                **page,
            )
        )
        review_tests.append(review_test)

    metadata = ReviewBundleMetadata(repository=repository, commit_sha=commit_sha, tests=review_tests)
    (bundle / "metadata.json").write_text(metadata.model_dump_json(indent=2, exclude_none=True) + "\n")
    page_tests = [
        review_test_page_data(
            test, asset_base=f"tests/{test.slug}/", page_url=f"tests/{test.slug}/index.html", shared_asset_base="../../"
        )
        for test in sorted(review_tests, key=test_impact_key)
    ]
    (bundle / "index.html").write_text(
        environment.get_template("pr_visuals.html.j2").render(
            repository=repository, commit_sha=commit_sha, gallery_stylesheet="gallery.css", tests=page_tests
        )
    )
    return bundle


def _upload_shared_image(s3: Any, *, bundle: Path, bucket: str, digest: str, cache_control: str) -> None:
    image_key = shared_image_key(digest)
    try:
        s3.head_object(Bucket=bucket, Key=image_key)
        return
    except ClientError as error:
        if not _is_not_found(error):
            raise
    try:
        s3.put_object(
            Bucket=bucket,
            Key=image_key,
            Body=(bundle.parent.parent / image_key).read_bytes(),
            ContentType="image/png",
            CacheControl=cache_control,
            IfNoneMatch="*",
        )
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") != "PreconditionFailed":
            raise


def upload_bundle(bundle: Path, *, endpoint: str, bucket: str, key: str, client: Any | None = None) -> None:
    s3 = client or boto3.client("s3", endpoint_url=endpoint)
    prefix = key.strip("/")
    cache_control = "public,max-age=31536000,immutable"
    metadata = ReviewBundleMetadata.model_validate_json((bundle / "metadata.json").read_text())
    digests = {
        digest
        for test in metadata.tests
        for asset in test.assets
        for digest in (
            asset.candidate_sha256,
            asset.baseline_sha256,
            asset.diff_sha256,
            *(variant.sha256 for variant in asset.variants),
        )
        if digest is not None
    }
    failures: list[Exception] = []
    logger.info("Checking and uploading %d shared images", len(digests))
    with ThreadPoolExecutor(max_workers=S3_IMAGE_UPLOAD_WORKERS) as executor:
        futures = [
            executor.submit(
                _upload_shared_image, s3, bundle=bundle, bucket=bucket, digest=digest, cache_control=cache_control
            )
            for digest in sorted(digests)
        ]
        # Drain every task before deciding whether review metadata may publish.
        # This keeps the index barrier intact even when image requests fail.
        for future in as_completed(futures):
            try:
                future.result()
            except Exception as error:
                failures.append(error)
    if failures:
        raise failures[0]
    paths = sorted(
        bundle.rglob("*"), key=lambda path: (path.name == "index.html", path == bundle / "index.html", path.as_posix())
    )
    logger.info("Uploading review metadata and pages")
    for path in paths:
        if not path.is_file():
            continue
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        relative = path.relative_to(bundle).as_posix()
        s3.upload_file(
            path, bucket, f"{prefix}/{relative}", ExtraArgs={"CacheControl": cache_control, "ContentType": content_type}
        )


def write_baseline_pointers(slugs: list[str], *, commit_sha: str, bucket: str, client: Any) -> None:
    """Point the current protocol's baseline at this commit for every published target.

    Runs on devel pushes after the immutable bundle upload succeeds, so PR runs
    can fall back to the newest devel bundle carrying each target (see
    :func:`_baseline_pointer_key`). Pointers are the bucket's only mutable
    objects — the commit bundles they reference stay immutable.
    """
    for slug in slugs:
        client.put_object(
            Bucket=bucket,
            Key=_baseline_pointer_key(slug),
            Body=BaselinePointer(commit_sha=commit_sha).model_dump_json().encode(),
            CacheControl="no-cache",
            ContentType="application/json",
        )


def diff_check(review_tests: list[ReviewTest]) -> tuple[Literal["success", "neutral"], str] | None:
    """Conclusion and summary for the `PR visual diffs` check-run.

    ``None`` when no target was compared against a baseline (devel pushes) —
    the check-run is simply absent then.
    Visual changes conclude ``neutral``, never ``failure``: the check is a
    review pointer, not a merge gate.
    """
    compared = [test for test in review_tests if test.summary is not None]
    if not compared:
        return None
    totals = classification_totals(review_tests)
    summary = (
        f"{totals.modified} modified, {totals.new} new, {totals.removed} removed, "
        f"{totals.unchanged} unchanged, {totals.unstable} unstable."
    )
    if incomplete := sum(1 for test in compared if test.issues):
        summary += f" {incomplete} target(s) have incomplete results."
    if fallbacks := sum(1 for test in compared if test.baseline_fallback):
        plural = "s" if fallbacks != 1 else ""
        summary += f" {fallbacks} target{plural} compared against the devel-latest fallback baseline."
    return (
        "neutral"
        if totals.modified + totals.removed + totals.unstable or any(t.issues for t in compared)
        else "success",
        summary,
    )


def current_workflow_url() -> str | None:
    server = os.environ.get("GITHUB_SERVER_URL")
    repository = os.environ.get("GITHUB_REPOSITORY")
    run_id = os.environ.get("GITHUB_RUN_ID")
    if not server or not repository or not run_id:
        return None
    return f"{server}/{repository}/actions/runs/{run_id}"


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser()
    result.add_argument("--ci-run-id", required=True)
    result.add_argument("--ci-run-attempt", required=True)
    result.add_argument("--work-dir", type=Path, required=True)
    result.add_argument("--sha", required=True)
    result.add_argument("--repository", required=True)
    result.add_argument("--endpoint", required=True)
    result.add_argument("--bucket", required=True)
    result.add_argument("--public-base-url", required=True)
    result.add_argument(
        "--pull-request-head",
        help="OWNER:BRANCH of the PR this run was built for, whose number and base commit are looked up; "
        "absent for a devel push",
    )
    result.add_argument("--check-external-id")
    result.add_argument("--ci-conclusion", default="success")
    result.add_argument("--ci-details-url")
    return result


def _invocations_for(
    args: argparse.Namespace, *, bbapi: Path = Path("bbapi"), run: Runner = subprocess.run
) -> list[str]:
    if not args.pull_request_head:
        # Devel pushes run a full sweep, so use this run's own invocations.
        return [
            str(invocation_id(run_id=args.ci_run_id, attempt=args.ci_run_attempt, role=role))
            for role in ("test", "build")
        ]
    return find_test_invocations(
        run_id=args.ci_run_id,
        run_attempt=args.ci_run_attempt,
        commit_sha=args.sha,
        repository=args.repository,
        bbapi=bbapi,
        run=run,
    )


@dataclass
class PublicationResult:
    summary: str = "No tests executed by Bazel CI exposed screenshot output records."
    conclusion: Literal["success", "failure", "neutral"] = "neutral"
    comment: CommentUpdate | None = None
    details_url: str | None = None
    ci_failures: list[str] = field(default_factory=list)
    pull_request: int | None = None
    diff: tuple[Literal["success", "neutral"], str] | None = None


def _publish(
    args: argparse.Namespace,
    *,
    token: str,
    workflow_url: str | None,
    result: PublicationResult,
    run: Runner,
    s3_client: Callable[..., Any],
) -> None:
    base_sha: str | None = None
    if args.pull_request_head:
        logger.info("Resolving pull request")
        found = find_reviewable_pull_request(
            repository=args.repository, head=args.pull_request_head, head_sha=args.sha, token=token
        )
        if found is None:
            result.summary = "No open or merged pull request has this head commit; nothing to review."
            print(result.summary)
            return
        result.pull_request, base_sha = found.number, found.base_sha
    logger.info("Finding test invocations")
    invocations = _invocations_for(args, run=run)
    test_id = str(invocation_id(run_id=args.ci_run_id, attempt=args.ci_run_attempt, role="test"))
    logger.info("Waiting for %d test invocation(s)", 1 if test_id in invocations else len(invocations))
    readiness = wait_for_test_invocations(
        [test_id] if test_id in invocations else invocations,
        workflow_succeeded=args.ci_conclusion == "success" and test_id in invocations,
        allow_build_only=args.ci_conclusion in ("success", "failure") and test_id in invocations,
        build_invocation_id=str(invocation_id(run_id=args.ci_run_id, attempt=args.ci_run_attempt, role="build")),
        run=run,
    )
    logger.info("Test invocation readiness: %s", readiness)
    if readiness == "incomplete":
        raise RuntimeError("Bazel test invocation did not complete before visual publication")
    if args.ci_conclusion != "success":
        result.ci_failures = list_ci_failures(invocations, run=run)
    tests = (
        download_visual_tests(invocations, args.work_dir / "tests", repository=args.repository, run=run)
        if readiness == "complete"
        else []
    )
    github_output = os.environ.get("GITHUB_OUTPUT")
    if github_output:
        with Path(github_output).open("a") as output:
            output.write(f"found={'true' if tests else 'false'}\n")
    if not tests:
        result.summary += ci_failure_summary(args.ci_conclusion, result.ci_failures)
        comment_state = CommentState(commit_sha=args.sha, kind="no_visual", ci_conclusion=args.ci_conclusion)
        no_visual_body = no_visual_comment_body(
            repository=args.repository,
            commit_sha=comment_state.commit_sha,
            ci_conclusion=comment_state.ci_conclusion,
            ci_failures=result.ci_failures,
            details_url=args.ci_details_url or workflow_url,
        )
        result.comment = CommentUpdate(body=no_visual_body, state=comment_state)
        print(f"{result.summary} Skipping publication.")
        return

    s3 = s3_client("s3", endpoint_url=args.endpoint)
    baseline_source: BaselineSource | None = None
    if base_sha:
        baseline_source = S3BaselineSource(client=s3, bucket=args.bucket)
    logger.info("Building review for %d targets; baseline comparison: %s", len(tests), bool(base_sha))
    bundle = build_bundle(
        tests,
        args.work_dir / "site",
        commit_sha=args.sha,
        repository=args.repository,
        base_sha=base_sha,
        baseline_source=baseline_source,
    )
    upload_bundle(bundle, endpoint=args.endpoint, bucket=args.bucket, key=f"{REVIEW_PREFIX}/{args.sha}", client=s3)
    review_tests = ReviewBundleMetadata.model_validate_json((bundle / "metadata.json").read_text()).tests
    if not base_sha:
        # Devel push: advance the mutable per-target fallback pointers now
        # that this commit's immutable bundle is fully uploaded and the
        # invocation is confirmed complete.
        slugs = [test.slug for test in review_tests if test.baseline_eligible]
        logger.info("Updating %d baseline pointers", len(slugs))
        write_baseline_pointers(slugs, commit_sha=args.sha, bucket=args.bucket, client=s3)
    public_url = f"{args.public_base_url.rstrip('/')}/{REVIEW_PREFIX}/{args.sha}/"
    if base_sha:
        totals = classification_totals(review_tests)
        result.summary = (
            f"{len(tests)} target{'s' if len(tests) != 1 else ''} · {totals.modified} modified, "
            f"{totals.new} new, {totals.removed} removed, {totals.unchanged} unchanged, "
            f"{totals.unstable} unstable."
        )
    else:
        result.summary = f"{len(tests)} Bazel test target{'s' if len(tests) != 1 else ''} produced visual artifacts."
    comment_state = CommentState(commit_sha=args.sha, kind="published", ci_conclusion=args.ci_conclusion)
    result.comment = CommentUpdate(
        body=success_comment_body(
            repository=args.repository,
            commit_sha=comment_state.commit_sha,
            url=public_url,
            review_tests=review_tests,
            base_sha=base_sha,
            ci_conclusion=comment_state.ci_conclusion,
            ci_failures=result.ci_failures,
            ci_details_url=args.ci_details_url or workflow_url,
        ),
        state=comment_state,
    )
    result.summary += ci_failure_summary(args.ci_conclusion, result.ci_failures)
    if incomplete := sum(1 for test in review_tests if test.issues):
        result.summary += f" {incomplete} target(s) have incomplete or non-comparable results."
    result.conclusion = "neutral" if any(test.issues for test in review_tests) else "success"
    result.details_url = f"{public_url}index.html"
    result.diff = diff_check(review_tests)


def _report(args: argparse.Namespace, *, token: str, result: PublicationResult) -> list[Exception]:
    errors: list[Exception] = []
    logger.info("Updating review comment and check")
    if result.diff is not None:
        diff_conclusion, diff_summary = result.diff
        try:
            upsert_check_run(
                repository=args.repository,
                commit_sha=args.sha,
                status="completed",
                conclusion=diff_conclusion,
                summary=diff_summary,
                details_url=result.details_url,
                token=token,
                name="PR visual diffs",
            )
        except Exception as error:
            logger.exception("Could not update the visual diff check")
            errors.append(error)
    comment_pr = result.pull_request
    if comment_pr is not None and result.comment is not None:
        # Rendering/uploading may outlast another push or PR closure. Recheck
        # before updating the singleton comment; immutable bundles stay useful.
        try:
            current = find_reviewable_pull_request(
                repository=args.repository, head=args.pull_request_head, head_sha=args.sha, token=token
            )
        except Exception as error:
            logger.exception("Could not recheck the pull request before commenting")
            errors.append(error)
            comment_pr = None
        else:
            if current is None or current.number != comment_pr:
                comment_pr = None
    if comment_pr is not None and result.comment is not None:
        try:
            publish_pull_request_comment(
                repository=args.repository, pull_request=comment_pr, update=result.comment, token=token
            )
        except Exception as error:
            logger.exception("Could not update the pull request comment")
            errors.append(error)
    if errors:
        result.conclusion = "failure"
        result.summary += " Pull request review reporting failed."
    try:
        upsert_check_run(
            repository=args.repository,
            commit_sha=args.sha,
            status="completed",
            conclusion=result.conclusion,
            summary=result.summary,
            details_url=result.details_url,
            external_id=args.check_external_id,
            token=token,
        )
    except Exception as error:
        logger.exception("Could not complete the visual review check")
        errors.append(error)
    logger.info("Visual publication finished: %s", result.conclusion)
    return errors


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(name)s: %(message)s")
    args = parser().parse_args()
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        raise ValueError("GITHUB_TOKEN is required to publish the visual-review check run")
    workflow_url = current_workflow_url()
    result = PublicationResult(details_url=args.ci_details_url or workflow_url)

    if args.ci_conclusion == "cancelled":
        # A direct CLI invocation must follow the workflow's cancelled-run policy too.
        # Close an announced check without reading BuildBuddy or touching S3.
        upsert_check_run(
            repository=args.repository,
            commit_sha=args.sha,
            status="completed",
            conclusion="cancelled",
            summary="Bazel CI was cancelled; visual publication stopped.",
            details_url=result.details_url,
            external_id=args.check_external_id,
            token=token,
        )
        return

    try:
        _publish(
            args, token=token, workflow_url=workflow_url, result=result, run=subprocess.run, s3_client=boto3.client
        )
    except Exception as error:
        result.summary = str(error) + ci_failure_summary(args.ci_conclusion, result.ci_failures)
        result.conclusion = "failure"
        reporting_errors: list[Exception] = []
        try:
            comment_state = CommentState(commit_sha=args.sha, kind="failure", ci_conclusion=args.ci_conclusion)
            result.comment = CommentUpdate(
                body=error_comment_body(
                    repository=args.repository,
                    commit_sha=comment_state.commit_sha,
                    error=error,
                    ci_conclusion=comment_state.ci_conclusion,
                    ci_failures=result.ci_failures,
                    ci_details_url=args.ci_details_url or workflow_url,
                ),
                state=comment_state,
            )
        except Exception as rendering_error:
            logger.exception("Could not render the failure comment")
            reporting_errors.append(rendering_error)
        reporting_errors.extend(_report(args, token=token, result=result))
        if reporting_errors:
            raise error from ExceptionGroup("Visual review reporting failed", reporting_errors)
        raise
    reporting_errors = _report(args, token=token, result=result)
    if reporting_errors:
        raise ExceptionGroup("Visual review reporting failed", reporting_errors)


if __name__ == "__main__":
    main()
