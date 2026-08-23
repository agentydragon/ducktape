"""Publish immutable visual-review bundles from Bazel tests run by trusted CI."""

from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import os
import re
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol, cast

import boto3
from botocore.exceptions import ClientError
from github import Auth, Github
from jinja2 import Environment, FileSystemLoader, select_autoescape
from pydantic import BaseModel, TypeAdapter

from devinfra.pr_visuals.check_run import upsert_check_run
from util.visual_diff import compare_pngs
from util.visual_review import MANIFEST_NAME, VisualReviewManifest

FULL_SHA = re.compile(r"^[0-9a-f]{40}$")
Runner = Callable[..., subprocess.CompletedProcess[str]]
COMMENT_MARKER = "<!-- pr-visuals -->"
COMMENT_BUDGET = 6000


class BazelInvocation(BaseModel):
    role: str
    invocation_id: str


class BuildBuddyLinkage(BaseModel):
    bazel_invocations: list[BazelInvocation]


class LinkageRecord(BaseModel):
    buildbuddy: BuildBuddyLinkage


class BuildBuddyArtifact(BaseModel):
    label: str
    name: str
    uri: str


@dataclass(frozen=True)
class ListedArtifact:
    invocation_id: str
    artifact: BuildBuddyArtifact


@dataclass(frozen=True)
class DownloadedVisualTest:
    target_label: str
    slug: str
    manifest: VisualReviewManifest
    directory: Path


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


def _baseline_key(base_sha: str, slug: str, *parts: str) -> str:
    """Object key for a baseline artifact at ``commits/<base_sha>/tests/<slug>/...``."""
    return "/".join(("commits", base_sha, "tests", slug, *parts))


def _baseline_pointer_key(slug: str) -> str:
    """Mutable per-target pointer to the last devel commit that published it.

    Devel-push publishes only contain targets Bazel actually re-executed
    (cache hits re-expose nothing), so a PR's base commit bundle routinely
    lacks untouched targets. The pointer bridges those gaps: it names the
    newest devel commit whose immutable ``commits/<sha>/…`` bundle carries
    the target, and PR runs fall back to it when the base bundle misses.
    """
    return f"baselines/{slug}.json"


class BaselinePointer(BaseModel):
    commit_sha: str


@dataclass(frozen=True)
class ResolvedBaseline:
    """Baseline bundle a candidate target is compared against."""

    commit_sha: str
    metadata: dict[str, Any]
    # True when the PR's base commit bundle lacked this target and the
    # devel-latest pointer supplied the baseline instead.
    fallback: bool


def _resolve_baseline(slug: str, base_sha: str | None, source: BaselineSource) -> ResolvedBaseline | None:
    def metadata_at(sha: str) -> dict[str, Any] | None:
        body = source.fetch(_baseline_key(sha, slug, "metadata.json"))
        return None if body is None else cast(dict[str, Any], json.loads(body))

    if base_sha is not None and (metadata := metadata_at(base_sha)) is not None:
        return ResolvedBaseline(commit_sha=base_sha, metadata=metadata, fallback=False)
    pointer_body = source.fetch(_baseline_pointer_key(slug))
    if pointer_body is None:
        return None
    pointer = BaselinePointer.model_validate_json(pointer_body)
    if (metadata := metadata_at(pointer.commit_sha)) is None:
        return None
    return ResolvedBaseline(commit_sha=pointer.commit_sha, metadata=metadata, fallback=True)


class ClassificationCounts(BaseModel):
    modified: int = 0
    new: int = 0
    removed: int = 0
    unchanged: int = 0


class ReviewAsset(BaseModel):
    """A visual-review asset enriched with its baseline comparison result."""

    path: str
    label: str
    classification: Literal["unchanged", "modified", "new", "removed"] | None = None
    changed_fraction: float | None = None
    changed_pixels: int | None = None
    candidate_dimensions: list[int] | None = None
    baseline_dimensions: list[int] | None = None
    dimension_changed: bool | None = None


class ReviewTest(BaseModel):
    """A test target's review data, serialized to per-test and commit metadata.

    `base_sha` is the commit whose bundle the comparison actually used —
    the PR's base commit, or (with `baseline_fallback=True`) the devel-latest
    pointer's commit when the base bundle lacked this target.
    """

    target_label: str
    slug: str
    title: str
    assets: list[ReviewAsset]
    base_sha: str | None = None
    baseline_fallback: bool | None = None
    summary: ClassificationCounts | None = None
    preview: ReviewAsset | None = None


class ReviewBundleMetadata(BaseModel):
    repository: str
    commit_sha: str
    tests: list[ReviewTest]


def find_test_invocations(linkage_dir: Path) -> list[str]:
    invocations: list[BazelInvocation] = []
    for path in sorted(linkage_dir.glob("*.json")):
        record = LinkageRecord.model_validate_json(path.read_text())
        invocations.extend(record.buildbuddy.bazel_invocations)
    if not invocations:
        raise ValueError("no Bazel invocations found in BuildBuddy linkage")
    ordered = sorted(invocations, key=lambda invocation: invocation.role != "test")
    return list(dict.fromkeys(invocation.invocation_id for invocation in ordered))


def list_ci_artifacts(
    invocations: list[str], *, bbapi: Path = Path("bbapi"), run: Runner = subprocess.run
) -> list[ListedArtifact]:
    listed: list[ListedArtifact] = []
    failures: list[str] = []
    for invocation in invocations:
        result = run([bbapi, "artifact", "list", invocation, "--json"], check=False, text=True, capture_output=True)
        if result.returncode != 0:
            failures.append(f"{invocation}: {result.stderr.strip()}")
            continue
        parsed_artifacts: list[BuildBuddyArtifact] | None = TypeAdapter(list[BuildBuddyArtifact] | None).validate_json(
            result.stdout
        )
        artifacts: list[BuildBuddyArtifact] = parsed_artifacts or []
        listed.extend(ListedArtifact(invocation, artifact) for artifact in artifacts)
    if not listed and len(failures) == len(invocations):
        raise RuntimeError("all BuildBuddy artifact queries failed: " + "; ".join(failures))
    return listed


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


def target_slug(label: str) -> str:
    readable = re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")
    if not readable:
        raise ValueError(f"cannot create URL slug for Bazel target {label!r}")
    digest = hashlib.sha256(label.encode()).hexdigest()[:10]
    return f"{readable[:80]}-{digest}"


def _download_artifact(listed: ListedArtifact, destination: Path, *, bbapi: Path, run: Runner) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    match = f"{listed.artifact.label}/{listed.artifact.name}"
    run([bbapi, "artifact", "download", listed.invocation_id, match, "-o", destination], check=True, text=True)


def download_visual_tests(
    invocations: list[str], destination: Path, *, bbapi: Path = Path("bbapi"), run: Runner = subprocess.run
) -> list[DownloadedVisualTest]:
    artifacts = list_ci_artifacts(invocations, bbapi=bbapi, run=run)
    by_target: dict[str, list[ListedArtifact]] = {}
    for listed in artifacts:
        by_target.setdefault(listed.artifact.label, []).append(listed)

    tests: list[DownloadedVisualTest] = []
    used_slugs: dict[str, str] = {}
    for target_label, target_artifacts in sorted(by_target.items()):
        manifests = [
            artifact for artifact in target_artifacts if artifact.artifact.name == f"test.outputs/{MANIFEST_NAME}"
        ]
        if not manifests:
            continue

        slug = target_slug(target_label)
        if previous := used_slugs.get(slug):
            raise ValueError(f"Bazel target URL slug collision: {previous} and {target_label}")
        used_slugs[slug] = target_label
        test_dir = destination / slug
        manifest_path = test_dir / MANIFEST_NAME

        # A bb remote script can expose the same test result through more than
        # one linked invocation (for example, a retry or a child invocation that
        # was discovered after the primary one).  Compare the manifests rather
        # than rejecting the target merely because it has duplicate listings.
        # Conflicting manifests remain an error: there is no honest way to pick
        # one candidate without a result-attempt identity.
        candidate_dir = destination / ".manifests" / slug
        candidates: list[tuple[ListedArtifact, VisualReviewManifest, Path]] = []
        for index, listed in enumerate(manifests):
            candidate_path = candidate_dir / f"{index}.json"
            _download_artifact(listed, candidate_path, bbapi=bbapi, run=run)
            candidates.append(
                (listed, VisualReviewManifest.model_validate_json(candidate_path.read_text()), candidate_path)
            )
        signatures = {json.dumps(manifest.model_dump(mode="json"), sort_keys=True) for _, manifest, _ in candidates}
        if len(signatures) != 1:
            raise ValueError(f"{target_label} exposed conflicting visual manifests from {len(manifests)} results")
        selected_listed, manifest, selected_path = candidates[0]
        test_dir.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(selected_path, manifest_path)

        available: dict[str, ListedArtifact] = {}
        for artifact in sorted(target_artifacts, key=lambda item: item.invocation_id != selected_listed.invocation_id):
            available.setdefault(artifact.artifact.name, artifact)
        for asset in manifest.assets:
            artifact_name = f"test.outputs/{asset.path}"
            asset_artifact = available.get(artifact_name)
            if asset_artifact is None:
                raise ValueError(f"{target_label} visual manifest references missing artifact {asset.path}")
            _download_artifact(asset_artifact, test_dir / asset.path, bbapi=bbapi, run=run)
        tests.append(DownloadedVisualTest(target_label, slug, manifest, test_dir))
    return tests


def _templates() -> Environment:
    return Environment(
        loader=FileSystemLoader(Path(__file__).parent),
        autoescape=select_autoescape(["html", "j2"]),
        keep_trailing_newline=True,
    )


def _asset_summary(assets: list[ReviewAsset]) -> ClassificationCounts:
    summary = ClassificationCounts()
    for asset in assets:
        match asset.classification:
            case "modified":
                summary.modified += 1
            case "new":
                summary.new += 1
            case "removed":
                summary.removed += 1
            case "unchanged":
                summary.unchanged += 1
    return summary


def _pick_preview(assets: list[ReviewAsset]) -> ReviewAsset | None:
    """The one asset shown as a test's thumbnail on the aggregate index page.

    Prefers a `modified` asset (there's a diff to draw attention to); falls back to a `new` one
    (a target whose assets are all new — e.g. its first publish, or a PR that only adds fixtures
    to an existing target — otherwise gets no thumbnail at all).
    """
    return next(
        (asset for asset in assets if asset.classification == "modified"),
        next((asset for asset in assets if asset.classification == "new"), None),
    )


def _classify_test_assets(
    test: DownloadedVisualTest, baseline: ResolvedBaseline, baseline_source: BaselineSource, target_dir: Path
) -> list[ReviewAsset]:
    """Classify a test's candidate assets against its resolved baseline.

    Writes ``baseline/<path>`` (modified + removed) and ``diff/<path>``
    (modified) PNGs into ``target_dir``. Candidate assets absent from the
    baseline are ``new``; baseline assets absent from the candidate are
    ``removed``; the rest are compared by :func:`compare_pngs`.
    """
    baseline_by_path = {asset["path"]: asset for asset in baseline.metadata["assets"]}
    baseline_dir = target_dir / "baseline"
    diff_dir = target_dir / "diff"
    enriched: list[ReviewAsset] = []

    def materialize_baseline(path: str) -> Path | None:
        body = baseline_source.fetch(_baseline_key(baseline.commit_sha, test.slug, path))
        if body is None:
            return None
        baseline_dir.mkdir(parents=True, exist_ok=True)
        destination = baseline_dir / path
        destination.write_bytes(body)
        return destination

    for asset in test.manifest.assets:
        if asset.path not in baseline_by_path:
            enriched.append(ReviewAsset(path=asset.path, label=asset.label, classification="new"))
            continue
        baseline_png = materialize_baseline(asset.path)
        if baseline_png is None:
            enriched.append(ReviewAsset(path=asset.path, label=asset.label, classification="new"))
            continue
        comparison = compare_pngs(test.directory / asset.path, baseline_png)
        if comparison.classification == "unchanged":
            enriched.append(ReviewAsset(path=asset.path, label=asset.label, classification="unchanged"))
            continue
        diff_dir.mkdir(parents=True, exist_ok=True)
        if comparison.diff_overlay is not None:
            comparison.diff_overlay.save(diff_dir / asset.path)
        enriched.append(
            ReviewAsset(
                path=asset.path,
                label=asset.label,
                classification="modified",
                changed_fraction=comparison.changed_fraction,
                changed_pixels=comparison.changed_pixels,
                candidate_dimensions=list(comparison.actual_size),
                baseline_dimensions=list(comparison.baseline_size),
                dimension_changed=comparison.dimension_changed,
            )
        )
    candidate_paths = {asset.path for asset in test.manifest.assets}
    for path, baseline_asset in baseline_by_path.items():
        if path in candidate_paths:
            continue
        if materialize_baseline(path) is not None:
            enriched.append(ReviewAsset(path=path, label=baseline_asset.get("label", path), classification="removed"))
    return enriched


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

    bundle = output_root / "commits" / commit_sha
    environment = _templates()
    page_tests: list[dict[str, Any]] = []
    review_tests: list[ReviewTest] = []
    for test in tests:
        target_dir = bundle / "tests" / test.slug
        target_dir.mkdir(parents=True, exist_ok=True)
        for asset in test.manifest.assets:
            shutil.copyfile(test.directory / asset.path, target_dir / asset.path)
        baseline: ResolvedBaseline | None = None
        if base_sha and baseline_source is not None:
            baseline = _resolve_baseline(test.slug, base_sha, baseline_source)
            if baseline is None:
                # No bundle carries this target yet — everything is new.
                assets = [
                    ReviewAsset(path=asset.path, label=asset.label, classification="new")
                    for asset in test.manifest.assets
                ]
            else:
                assets = _classify_test_assets(test, baseline, baseline_source, target_dir)
        else:
            assets = [ReviewAsset(path=asset.path, label=asset.label) for asset in test.manifest.assets]
        review_test = ReviewTest(
            target_label=test.target_label,
            slug=test.slug,
            title=test.manifest.title,
            assets=assets,
            base_sha=baseline.commit_sha if baseline is not None else base_sha,
            baseline_fallback=baseline.fallback if baseline is not None else None,
            summary=_asset_summary(assets) if base_sha else None,
            preview=_pick_preview(assets) if base_sha else None,
        )
        (target_dir / "metadata.json").write_text(review_test.model_dump_json(indent=2, exclude_none=True) + "\n")
        page = review_test.model_dump(exclude_none=True)
        (target_dir / "index.html").write_text(
            environment.get_template("pr_visual_test.html.j2").render(
                repository=repository, commit_sha=commit_sha, **page
            )
        )
        page_tests.append(page)
        review_tests.append(review_test)

    bundle.mkdir(parents=True, exist_ok=True)
    metadata = ReviewBundleMetadata(repository=repository, commit_sha=commit_sha, tests=review_tests)
    (bundle / "metadata.json").write_text(metadata.model_dump_json(indent=2, exclude_none=True) + "\n")
    (bundle / "index.html").write_text(
        environment.get_template("pr_visuals.html.j2").render(
            repository=repository, commit_sha=commit_sha, tests=page_tests
        )
    )
    return bundle


def upload_bundle(bundle: Path, *, endpoint: str, bucket: str, key: str, client: Any | None = None) -> None:
    s3 = client or boto3.client("s3", endpoint_url=endpoint)
    prefix = key.strip("/")
    cache_control = "public,max-age=31536000,immutable"
    paths = sorted(
        bundle.rglob("*"), key=lambda path: (path.name == "index.html", path == bundle / "index.html", path.as_posix())
    )
    for path in paths:
        if not path.is_file():
            continue
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        relative = path.relative_to(bundle).as_posix()
        s3.upload_file(
            path, bucket, f"{prefix}/{relative}", ExtraArgs={"CacheControl": cache_control, "ContentType": content_type}
        )


def write_baseline_pointers(slugs: list[str], *, commit_sha: str, bucket: str, client: Any) -> None:
    """Point ``baselines/<slug>.json`` at this commit for every published target.

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


def _totals(review_tests: list[ReviewTest]) -> ClassificationCounts:
    totals = ClassificationCounts()
    for test in review_tests:
        if test.summary is not None:
            totals.modified += test.summary.modified
            totals.new += test.summary.new
            totals.removed += test.summary.removed
            totals.unchanged += test.summary.unchanged
    return totals


def _preview_img(url: str) -> str:
    """One before/after/diff table cell; 3 × 260px fits GitHub's comment width."""
    return f'<img src="{url}" width="260">'


def _with_diff_previews(base: str, review_tests: list[ReviewTest], url: str) -> str:
    """Append up to two modified-asset before/after/diff tables and up to two new-asset
    previews, respecting the byte budget."""
    modified = [
        (asset.changed_fraction or 0.0, test.slug, asset)
        for test in review_tests
        for asset in test.assets
        if asset.classification == "modified"
    ]
    modified.sort(key=lambda item: item[0], reverse=True)
    new = [(test.slug, asset) for test in review_tests for asset in test.assets if asset.classification == "new"]
    if not modified and not new:
        return base
    for limit in (2, 1):
        lines: list[str] = []
        if modified:
            lines += ["", "### Top changes"]
            for fraction, slug, asset in modified[:limit]:
                test_url = f"{url}tests/{slug}"
                # Dimension changes produce no diff overlay (the images can't be
                # compared pixel-for-pixel), so that cell degrades to text.
                diff_cell = (
                    _preview_img(f"{test_url}/diff/{asset.path}")
                    if not asset.dimension_changed
                    else "_(dimensions changed)_"
                )
                lines += [
                    "",
                    f"`{asset.label}` · {fraction:.1%} changed",
                    "",
                    "| Before | After | Diff |",
                    "| --- | --- | --- |",
                    f"| {_preview_img(f'{test_url}/baseline/{asset.path}')} "
                    f"| {_preview_img(f'{test_url}/{asset.path}')} "
                    f"| {diff_cell} |",
                ]
        if new:
            # No baseline to compare against — one image each, not a before/after/diff table.
            lines += ["", "### New screenshots"]
            for slug, asset in new[:limit]:
                test_url = f"{url}tests/{slug}"
                lines += ["", f"`{asset.label}`", "", _preview_img(f"{test_url}/{asset.path}")]
        body = base + "\n" + "\n".join(lines)
        if len(body) <= COMMENT_BUDGET:
            return body
    return base


def _format_test_counts(counts: ClassificationCounts) -> str:
    """`"4 modified, 12 new"` — only the non-zero buckets, so an untouched target's bullet line
    doesn't read `"0 modified, 0 new, 0 removed"`. `"unchanged"` when every bucket is zero."""
    parts = [
        f"{count} {label}"
        for count, label in ((counts.modified, "modified"), (counts.new, "new"), (counts.removed, "removed"))
        if count
    ]
    return ", ".join(parts) if parts else "unchanged"


def _ci_failure_notice(ci_conclusion: str, ci_failures: list[str]) -> list[str]:
    if ci_conclusion == "success":
        return []
    lines = ["> [!WARNING]", f"> Bazel CI concluded `{ci_conclusion}`; visual artifacts that arrived are shown below."]
    if ci_failures:
        lines += [">", "> Failed Bazel targets:"]
        displayed = ci_failures[:20]
        lines.extend(f"> - `{target}`" for target in displayed)
        if len(ci_failures) > len(displayed):
            lines.append(f"> - … and {len(ci_failures) - len(displayed)} more")
    else:
        lines.append("> Individual failed target labels were not available from BuildBuddy.")
    return lines


def _ci_failure_summary(ci_conclusion: str, ci_failures: list[str]) -> str:
    if ci_conclusion == "success":
        return ""
    if ci_failures:
        return f" Bazel CI concluded {ci_conclusion}; {len(ci_failures)} target(s) failed."
    return f" Bazel CI concluded {ci_conclusion}; failed target details were unavailable."


def success_comment_body(
    *,
    repository: str,
    commit_sha: str,
    url: str,
    review_tests: list[ReviewTest],
    base_sha: str | None = None,
    ci_conclusion: str = "success",
    ci_failures: list[str] | None = None,
) -> str:
    ci_failures = ci_failures or []
    commit_url = f"https://github.com/{repository}/commit/{commit_sha}"
    page_url = f"{url}index.html"
    target_count = len(review_tests)
    plural = "" if target_count == 1 else "s"
    lines = [COMMENT_MARKER, f"## Visual review for [`{commit_sha[:8]}`]({commit_url})", ""]
    if notice := _ci_failure_notice(ci_conclusion, ci_failures):
        lines += [*notice, ""]
    if base_sha is None:
        lines += [
            f"{target_count} Bazel test target{plural} produced visual artifacts. [Open visual review]({page_url}).",
            "",
        ]
        lines.extend(
            f"- [`{test.target_label}`]({url}tests/{test.slug}/index.html): {len(test.assets)} assets"
            for test in review_tests
        )
        return "\n".join(lines)

    totals = _totals(review_tests)
    if totals.modified == totals.new == totals.removed == 0:
        lines.append(
            f"No visual changes among the {target_count} affected Bazel test target{plural}. "
            f"[Open visual review]({page_url})."
        )
        return "\n".join(lines)

    lines += [
        f"{target_count} Bazel test target{plural} produced visual artifacts · "
        f"**{totals.modified} modified**, {totals.new} new, {totals.removed} removed, "
        f"{totals.unchanged} unchanged. [Open visual review]({page_url}).",
        "",
    ]
    for test in review_tests:
        counts = test.summary or ClassificationCounts()
        lines.append(f"- [`{test.target_label}`]({url}tests/{test.slug}/index.html): {_format_test_counts(counts)}")
    return _with_diff_previews("\n".join(lines), review_tests, url)


def no_visual_comment_body(
    *, repository: str, commit_sha: str, ci_conclusion: str, ci_failures: list[str], details_url: str | None
) -> str:
    commit_url = f"https://github.com/{repository}/commit/{commit_sha}"
    lines = [COMMENT_MARKER, f"## Visual review for [`{commit_sha[:8]}`]({commit_url})", ""]
    if notice := _ci_failure_notice(ci_conclusion, ci_failures):
        lines += [*notice, ""]
    lines.append("No visual artifacts were available from the Bazel CI run.")
    if details_url:
        lines.append(f"[Open the CI run]({details_url}) for the complete test results.")
    return "\n".join(lines)


def error_comment_body(
    *,
    repository: str,
    commit_sha: str,
    error: Exception,
    ci_conclusion: str = "success",
    ci_failures: list[str] | None = None,
) -> str:
    commit_url = f"https://github.com/{repository}/commit/{commit_sha}"
    message = str(error).replace("```", "'''")[:2000]
    lines = [COMMENT_MARKER, f"## Visual review failed for [`{commit_sha[:8]}`]({commit_url})", ""]
    if notice := _ci_failure_notice(ci_conclusion, ci_failures or []):
        lines += [*notice, ""]
    lines += [
        "> [!CAUTION]",
        "> The visual-review publisher failed while processing this Bazel CI run.",
        "",
        "```text",
        message,
        "```",
    ]
    return "\n".join(lines)


def _is_current_success_comment(*, body: str, repository: str, commit_sha: str) -> bool:
    commit_url = f"https://github.com/{repository}/commit/{commit_sha}"
    return f"## Visual review for [`{commit_sha[:8]}`]({commit_url})" in body and "[Open visual review](" in body


def _find_pull_request_comment(issue: Any) -> Any | None:
    return next(
        (
            comment
            for comment in issue.get_comments()
            if comment.user is not None and comment.user.type == "Bot" and COMMENT_MARKER in (comment.body or "")
        ),
        None,
    )


def upsert_pull_request_comment(*, repository: str, pull_request: int, body: str, token: str) -> None:
    with Github(auth=Auth.Token(token)) as github:
        issue = github.get_repo(repository).get_issue(pull_request)
        existing = _find_pull_request_comment(issue)
        if existing is None:
            issue.create_comment(body)
        else:
            existing.edit(body)


def refresh_stale_pull_request_comment(
    *, repository: str, pull_request: int, commit_sha: str, body: str, token: str
) -> None:
    """Refresh a singleton comment after a successful run exposed no visual artifacts.

    A cache-hit rerun has no manifest to republish.  Preserve a useful successful
    review already published for this exact SHA, but do not leave a failure or a
    previous head's result attached to the PR.  With no existing singleton there
    is nothing stale to correct, so the no-artifact run remains comment-free.
    """
    with Github(auth=Auth.Token(token)) as github:
        issue = github.get_repo(repository).get_issue(pull_request)
        existing = _find_pull_request_comment(issue)
        if existing is not None and not _is_current_success_comment(
            body=existing.body or "", repository=repository, commit_sha=commit_sha
        ):
            existing.edit(body)


def diff_check(review_tests: list[ReviewTest]) -> tuple[Literal["success", "neutral"], str] | None:
    """Conclusion and summary for the `PR visual diffs` check-run.

    ``None`` when no target was compared against a baseline (devel pushes, or
    a run without ``--base-sha``) — the check-run is simply absent then.
    Visual changes conclude ``neutral``, never ``failure``: the check is a
    review pointer, not a merge gate.
    """
    compared = [test for test in review_tests if test.summary is not None]
    if not compared:
        return None
    totals = _totals(review_tests)
    summary = f"{totals.modified} modified, {totals.new} new, {totals.removed} removed, {totals.unchanged} unchanged."
    if fallbacks := sum(1 for test in compared if test.baseline_fallback):
        plural = "s" if fallbacks != 1 else ""
        summary += f" {fallbacks} target{plural} compared against the devel-latest fallback baseline."
    return ("neutral" if totals.modified + totals.removed else "success", summary)


def current_workflow_url() -> str | None:
    server = os.environ.get("GITHUB_SERVER_URL")
    repository = os.environ.get("GITHUB_REPOSITORY")
    run_id = os.environ.get("GITHUB_RUN_ID")
    if not server or not repository or not run_id:
        return None
    return f"{server}/{repository}/actions/runs/{run_id}"


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser()
    result.add_argument("--linkage-dir", type=Path, required=True)
    result.add_argument("--work-dir", type=Path, required=True)
    result.add_argument("--sha", required=True)
    result.add_argument("--base-sha")
    result.add_argument("--repository", required=True)
    result.add_argument("--endpoint", required=True)
    result.add_argument("--bucket", required=True)
    result.add_argument("--public-base-url", required=True)
    result.add_argument("--pull-request", type=int)
    result.add_argument("--check-external-id")
    result.add_argument("--ci-conclusion", default="success")
    result.add_argument("--ci-details-url")
    return result


def main() -> None:
    args = parser().parse_args()
    github_token = os.environ.get("GITHUB_TOKEN")
    if not github_token:
        raise ValueError("GITHUB_TOKEN is required to publish the visual-review check run")
    workflow_url = current_workflow_url()

    conclusion: Literal["success", "failure", "neutral"] = "neutral"
    summary = "No tests executed by Bazel CI exposed visual-review.json."
    comment_body: str | None = None
    refresh_stale_comment_body: str | None = None
    details_url: str | None = args.ci_details_url or workflow_url
    ci_failures: list[str] = []
    try:
        linkage_files = list(args.linkage_dir.glob("*.json"))
        invocations = find_test_invocations(args.linkage_dir) if linkage_files else []
        if args.ci_conclusion != "success":
            ci_failures = list_ci_failures(invocations)
        tests = download_visual_tests(invocations, args.work_dir / "tests") if invocations else []
        github_output = os.environ.get("GITHUB_OUTPUT")
        if github_output:
            with Path(github_output).open("a") as output:
                output.write(f"found={'true' if tests else 'false'}\n")
        if not tests:
            summary += _ci_failure_summary(args.ci_conclusion, ci_failures)
            no_visual_body = no_visual_comment_body(
                repository=args.repository,
                commit_sha=args.sha,
                ci_conclusion=args.ci_conclusion,
                ci_failures=ci_failures,
                details_url=args.ci_details_url or workflow_url,
            )
            if args.ci_conclusion == "success":
                refresh_stale_comment_body = no_visual_body
            else:
                comment_body = no_visual_body
            print(f"{summary} Skipping publication.")
            return

        s3 = boto3.client("s3", endpoint_url=args.endpoint)
        base_sha = args.base_sha or None
        baseline_source: BaselineSource | None = None
        if base_sha:
            if not FULL_SHA.fullmatch(base_sha):
                raise ValueError("--base-sha must be the full 40-character lowercase SHA-1")
            baseline_source = S3BaselineSource(client=s3, bucket=args.bucket)
        bundle = build_bundle(
            tests,
            args.work_dir / "site",
            commit_sha=args.sha,
            repository=args.repository,
            base_sha=base_sha,
            baseline_source=baseline_source,
        )
        upload_bundle(bundle, endpoint=args.endpoint, bucket=args.bucket, key=f"commits/{args.sha}", client=s3)
        if not base_sha:
            # Devel push: advance the mutable per-target fallback pointers now
            # that this commit's immutable bundle is fully uploaded.
            write_baseline_pointers([test.slug for test in tests], commit_sha=args.sha, bucket=args.bucket, client=s3)
        public_url = f"{args.public_base_url.rstrip('/')}/commits/{args.sha}/"
        review_tests = ReviewBundleMetadata.model_validate_json((bundle / "metadata.json").read_text()).tests
        if base_sha:
            totals = _totals(review_tests)
            summary = (
                f"{len(tests)} target{'s' if len(tests) != 1 else ''} · {totals.modified} modified, "
                f"{totals.new} new, {totals.removed} removed, {totals.unchanged} unchanged."
            )
        else:
            summary = f"{len(tests)} Bazel test target{'s' if len(tests) != 1 else ''} produced visual artifacts."
        comment_body = success_comment_body(
            repository=args.repository,
            commit_sha=args.sha,
            url=public_url,
            review_tests=review_tests,
            base_sha=base_sha,
            ci_conclusion=args.ci_conclusion,
            ci_failures=ci_failures,
        )
        summary += _ci_failure_summary(args.ci_conclusion, ci_failures)
        conclusion, details_url = "success", f"{public_url}index.html"
        if (diff := diff_check(review_tests)) is not None:
            diff_conclusion, diff_summary = diff
            upsert_check_run(
                repository=args.repository,
                commit_sha=args.sha,
                status="completed",
                conclusion=diff_conclusion,
                summary=diff_summary,
                details_url=details_url,
                token=github_token,
                name="PR visual diffs",
            )
    except Exception as error:
        comment_body = error_comment_body(
            repository=args.repository,
            commit_sha=args.sha,
            error=error,
            ci_conclusion=args.ci_conclusion,
            ci_failures=ci_failures,
        )
        summary = str(error) + _ci_failure_summary(args.ci_conclusion, ci_failures)
        conclusion = "failure"
        raise
    finally:
        if comment_body is not None and args.pull_request is not None:
            upsert_pull_request_comment(
                repository=args.repository, pull_request=args.pull_request, body=comment_body, token=github_token
            )
        elif refresh_stale_comment_body is not None and args.pull_request is not None:
            refresh_stale_pull_request_comment(
                repository=args.repository,
                pull_request=args.pull_request,
                commit_sha=args.sha,
                body=refresh_stale_comment_body,
                token=github_token,
            )
        upsert_check_run(
            repository=args.repository,
            commit_sha=args.sha,
            status="completed",
            conclusion=conclusion,
            summary=summary,
            details_url=details_url,
            external_id=args.check_external_id,
            token=github_token,
        )


if __name__ == "__main__":
    main()
