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
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol, cast

import boto3
from botocore.exceptions import ClientError
from github import Auth, Github
from jinja2 import Environment, FileSystemLoader, select_autoescape
from pydantic import BaseModel, TypeAdapter
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from devinfra.ci.invocation_ids import invocation_id
from devinfra.pr_visuals.check_run import upsert_check_run
from util.visual_diff import compare_pngs
from util.visual_review import MANIFEST_NAME, VisualReviewManifest

FULL_SHA = re.compile(r"^[0-9a-f]{40}$")
Runner = Callable[..., subprocess.CompletedProcess[str]]
COMMENT_MARKER = "<!-- pr-visuals -->"
COMMENT_BUDGET = 6000


class BuildBuddyArtifact(BaseModel):
    label: str
    name: str
    uri: str


@dataclass(frozen=True)
class ListedArtifact:
    invocation_id: str
    artifact: BuildBuddyArtifact


@dataclass(frozen=True)
class _Download:
    listed: ListedArtifact
    destination: Path


@dataclass(frozen=True)
class _PlannedTest:
    target_label: str
    slug: str
    manifests: list[_Download]


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


BUILDBUDDY_APP = "https://app.buildbuddy.io"
BUILDBUDDY_RPC = f"{BUILDBUDDY_APP}/rpc/BuildBuddyService"
REPO_URL = "https://github.com/agentydragon/ducktape"

Fetcher = Callable[[urllib.request.Request], bytes]
# Measured against BuildBuddy: 40 blobs took 2.3s at this width, so a commit's ~294
# files land in under 20s. Raising it buys little and is less polite to the service.
DOWNLOAD_WORKERS = 8


def search_ci_test_invocations(commit_sha: str, *, api_key: str, fetch: Fetcher) -> list[str]:
    """The CI test invocations BuildBuddy holds for `commit_sha`, full sweep first.

    A commit has several CI runs with different target sets — a `//...` devel sweep
    alongside affected-set runs — and only the sweep carries the visual manifests.
    Asking by commit finds whichever run actually did the testing; asking by the run
    that triggered this publish finds whichever one happens to have triggered it.
    """
    request = urllib.request.Request(
        f"{BUILDBUDDY_RPC}/SearchInvocation",
        data=json.dumps(
            {"query": {"repoUrl": REPO_URL, "commitSha": commit_sha, "command": "test", "role": ["CI"]}, "count": 25}
        ).encode(),
        headers={"Content-Type": "application/json", "x-buildbuddy-api-key": api_key},
    )
    found = json.loads(fetch(request)).get("invocation", [])
    sweeps = [i["invocationId"] for i in found if i.get("pattern") == ["//..."]]
    return sweeps or [i["invocationId"] for i in found]


def _is_transient(error: BaseException) -> bool:
    if isinstance(error, urllib.error.HTTPError):
        return error.code >= 500
    return isinstance(error, urllib.error.URLError | TimeoutError)


@retry(retry=retry_if_exception(_is_transient), stop=stop_after_attempt(4), wait=wait_exponential(max=8), reraise=True)
def _read(request: urllib.request.Request) -> bytes:
    with urllib.request.urlopen(request, timeout=30) as response:
        body: bytes = response.read()
    return body


def find_test_invocations(*, run_id: str, run_attempt: str, commit_sha: str, api_key: str, fetch: Fetcher) -> list[str]:
    """Where this commit's test artifacts are, by commit if BuildBuddy knows, else by run.

    The by-run derivation (devinfra/ci/invocation_ids.py) is what makes a *cancelled*
    run addressable, and it is the only handle on a PR run, which records the merge SHA
    and so cannot be found by head SHA. It is the fallback rather than the primary
    because it names the run that triggered this publish, which is frequently not the
    run that ran the tests.

    The two are not combined: `download_visual_tests` rejects a target whose manifests
    disagree across invocations, and mixing a sweep with an affected-set run invites
    exactly that.
    """
    if found := search_ci_test_invocations(commit_sha, api_key=api_key, fetch=fetch):
        return found
    return [str(invocation_id(run_id=run_id, attempt=run_attempt, role=role)) for role in ("test", "build")]


# BuildBuddy's reply for an invocation ID it has never seen. Invocation IDs are assigned
# before the run (devinfra/ci/invocation_ids.py), so this is an ordinary state — a run
# cancelled before Bazel started names two invocations that never existed — and must not
# be confused with a query that genuinely failed.
_INVOCATION_ABSENT = "invocation not found"


def list_ci_artifacts(
    invocations: list[str], *, bbapi: Path = Path("bbapi"), run: Runner = subprocess.run
) -> list[ListedArtifact]:
    listed: list[ListedArtifact] = []
    failures: list[str] = []
    for invocation in invocations:
        result = run([bbapi, "artifact", "list", invocation, "--json"], check=False, text=True, capture_output=True)
        if result.returncode != 0 or _INVOCATION_ABSENT in result.stderr:
            if _INVOCATION_ABSENT not in result.stderr:
                failures.append(f"{invocation}: {result.stderr.strip()}")
            continue
        parsed_artifacts: list[BuildBuddyArtifact] | None = TypeAdapter(list[BuildBuddyArtifact] | None).validate_json(
            result.stdout
        )
        artifacts: list[BuildBuddyArtifact] = parsed_artifacts or []
        listed.extend(ListedArtifact(invocation, artifact) for artifact in artifacts)
    if failures and len(failures) == len(invocations):
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


def _download_artifact(download: _Download, *, api_key: str, fetch: Fetcher) -> None:
    """Read one artifact's blob straight from the CAS, by the URI the listing gave.

    Deviation from `bbapi artifact download`, which resolves a `label/name` pattern
    against the invocation's whole build event stream and so pays for that stream per
    file — README.md § Gotcha: artifacts come from the CAS.
    """
    download.destination.parent.mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(
        f"{BUILDBUDDY_APP}/file/download?bytestream_url={urllib.parse.quote(download.listed.artifact.uri, safe='')}",
        headers={"x-buildbuddy-api-key": api_key},
    )
    download.destination.write_bytes(fetch(request))


def _download_all(downloads: list[_Download], *, api_key: str, fetch: Fetcher) -> None:
    with ThreadPoolExecutor(max_workers=DOWNLOAD_WORKERS) as pool:
        pending = [pool.submit(_download_artifact, download, api_key=api_key, fetch=fetch) for download in downloads]
    for download in pending:
        download.result()


def download_visual_tests(
    invocations: list[str],
    destination: Path,
    *,
    api_key: str,
    fetch: Fetcher = _read,
    bbapi: Path = Path("bbapi"),
    run: Runner = subprocess.run,
) -> list[DownloadedVisualTest]:
    artifacts = list_ci_artifacts(invocations, bbapi=bbapi, run=run)
    by_target: dict[str, list[ListedArtifact]] = {}
    for listed in artifacts:
        by_target.setdefault(listed.artifact.label, []).append(listed)

    # A bb remote script can expose the same test result through more than one linked
    # invocation (for example, a retry or a child invocation that was discovered after
    # the primary one).  Compare the manifests rather than rejecting the target merely
    # because it has duplicate listings.  Conflicting manifests remain an error: there
    # is no honest way to pick one candidate without a result-attempt identity.
    planned: list[_PlannedTest] = []
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
        candidate_dir = destination / ".manifests" / slug
        planned.append(
            _PlannedTest(
                target_label,
                slug,
                [_Download(listed, candidate_dir / f"{index}.json") for index, listed in enumerate(manifests)],
            )
        )

    # Two waves rather than one, because the manifests are what name the assets: every
    # manifest has to land before any asset can be asked for.
    _download_all([download for test in planned for download in test.manifests], api_key=api_key, fetch=fetch)

    tests: list[DownloadedVisualTest] = []
    asset_downloads: list[_Download] = []
    for plan in planned:
        parsed = [
            VisualReviewManifest.model_validate_json(download.destination.read_text()) for download in plan.manifests
        ]
        signatures = {json.dumps(manifest.model_dump(mode="json"), sort_keys=True) for manifest in parsed}
        if len(signatures) != 1:
            raise ValueError(
                f"{plan.target_label} exposed conflicting visual manifests from {len(plan.manifests)} results"
            )
        selected, manifest = plan.manifests[0], parsed[0]
        test_dir = destination / plan.slug
        test_dir.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(selected.destination, test_dir / MANIFEST_NAME)

        available: dict[str, ListedArtifact] = {}
        for artifact in sorted(
            by_target[plan.target_label], key=lambda item: item.invocation_id != selected.listed.invocation_id
        ):
            available.setdefault(artifact.artifact.name, artifact)
        for asset in manifest.assets:
            artifact_name = f"test.outputs/{asset.path}"
            asset_artifact = available.get(artifact_name)
            if asset_artifact is None:
                raise ValueError(f"{plan.target_label} visual manifest references missing artifact {asset.path}")
            asset_downloads.append(_Download(asset_artifact, test_dir / asset.path))
        tests.append(DownloadedVisualTest(plan.target_label, plan.slug, manifest, test_dir))
    _download_all(asset_downloads, api_key=api_key, fetch=fetch)
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


def _previews(review_tests: list[ReviewTest], url: str, limit: int) -> list[str]:
    """Up to `limit` modified-asset before/after/diff tables, then up to `limit` new-asset
    previews; the lines to append after the target list."""
    modified = [
        (asset.changed_fraction or 0.0, test.slug, asset)
        for test in review_tests
        for asset in test.assets
        if asset.classification == "modified"
    ]
    modified.sort(key=lambda item: item[0], reverse=True)
    new = [(test.slug, asset) for test in review_tests for asset in test.assets if asset.classification == "new"]
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
    return lines


def _target_list(review_tests: list[ReviewTest], url: str, *, collapse_unchanged: bool) -> list[str]:
    """One bullet per target with changes; the unchanged ones folded under a `<details>` so the
    list reads as what changed, and can be dropped to a count when the budget is tight."""

    def bullet(test: ReviewTest) -> str:
        counts = test.summary or ClassificationCounts()
        return f"- [`{test.target_label}`]({url}tests/{test.slug}/index.html): {_format_test_counts(counts)}"

    changed = [
        test for test in review_tests if _format_test_counts(test.summary or ClassificationCounts()) != "unchanged"
    ]
    unchanged = [test for test in review_tests if test not in changed]
    lines = [bullet(test) for test in changed]
    if unchanged:
        plural = "" if len(unchanged) == 1 else "s"
        if collapse_unchanged:
            lines += [
                "",
                "<details>",
                f"<summary>{len(unchanged)} unchanged target{plural}</summary>",
                "",
                *(bullet(test) for test in unchanged),
                "",
                "</details>",
            ]
        else:
            lines += ["", f"{len(unchanged)} unchanged target{plural}."]
    return lines


def _with_target_list_and_previews(head: list[str], review_tests: list[ReviewTest], url: str) -> str:
    """The comment within budget: previews are what a reviewer opens the comment for, so the
    collapsed unchanged list goes first when something has to give, then the preview count."""
    for collapse_unchanged, limit in ((True, 2), (True, 1), (False, 2), (False, 1)):
        body = "\n".join(
            [
                *head,
                *_target_list(review_tests, url, collapse_unchanged=collapse_unchanged),
                *_previews(review_tests, url, limit),
            ]
        )
        if len(body) <= COMMENT_BUDGET:
            return body
    return "\n".join([*head, *_target_list(review_tests, url, collapse_unchanged=False)])


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


def _fallback_baseline_notice(review_tests: list[ReviewTest]) -> list[str]:
    """Explain when a PR comparison could not use its exact base commit.

    A per-target devel pointer is useful for showing a newly introduced visual
    target, but it can lag the PR base.  Its differences must not be presented
    as changes attributable solely to the PR.
    """
    fallbacks = [test for test in review_tests if test.baseline_fallback]
    if not fallbacks:
        return []
    baselines = sorted({test.base_sha[:8] for test in fallbacks if test.base_sha})
    target_count = len(fallbacks)
    target_word = "target" if target_count == 1 else "targets"
    baseline_word = "baseline" if len(baselines) == 1 else "baselines"
    baseline_list = ", ".join(f"`{baseline}`" for baseline in baselines) or "an unavailable commit"
    return [
        "> [!WARNING]",
        "> The exact PR-base visual baseline was unavailable. "
        f"{target_count} {target_word} used the latest published devel {baseline_word} ({baseline_list}) instead.",
        "> These differences may include visual changes already present on devel and are not attributable solely to this PR.",
    ]


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
    if notice := _fallback_baseline_notice(review_tests):
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
    return _with_target_list_and_previews(lines, review_tests, url)


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
    result.add_argument("--ci-run-id", required=True)
    result.add_argument("--ci-run-attempt", required=True)
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


def _buildbuddy_api_key() -> str:
    key = os.environ.get("BUILDBUDDY_API_KEY")
    if not key:
        raise ValueError("BUILDBUDDY_API_KEY is required to read this commit's test artifacts from BuildBuddy")
    return key


def _invocations_for(args: argparse.Namespace, *, api_key: str) -> list[str]:
    return find_test_invocations(
        run_id=args.ci_run_id, run_attempt=args.ci_run_attempt, commit_sha=args.sha, api_key=api_key, fetch=_read
    )


def publish_only(args: argparse.Namespace, *, github_token: str, api_key: str, details_url: str | None) -> None:
    """Upload a superseded run's commit bundle, saying nothing about it.

    Deliberately does not call :func:`write_baseline_pointers`. Pointers are the
    bucket's only mutable objects, `put_object` has no ordering guard, and the
    publish workflow's concurrency group is keyed on `head_sha` — so a superseded
    run and the run that superseded it do not serialize against each other, and a
    slow superseded publish could walk a pointer backwards onto an older commit.
    The immutable bundle is what makes an exact-baseline lookup succeed; the
    superseding run advances the pointer moments later.
    """
    # Before publishing, not after: this terminates the check the `announce` job left
    # `in_progress`, and a publish that then fails must not strand it there.
    upsert_check_run(
        repository=args.repository,
        commit_sha=args.sha,
        status="completed",
        conclusion="neutral",
        summary="Superseded by a newer commit before Bazel CI finished.",
        details_url=details_url,
        external_id=args.check_external_id,
        token=github_token,
    )
    invocations = _invocations_for(args, api_key=api_key)
    tests = download_visual_tests(invocations, args.work_dir / "tests", api_key=api_key)
    if tests:
        s3 = boto3.client("s3", endpoint_url=args.endpoint)
        bundle = build_bundle(
            tests, args.work_dir / "site", commit_sha=args.sha, repository=args.repository, base_sha=None
        )
        upload_bundle(bundle, endpoint=args.endpoint, bucket=args.bucket, key=f"commits/{args.sha}", client=s3)
        print(f"Superseded run on {args.sha}: published {len(tests)} visual target(s); pointers left alone.")
    else:
        print(f"Superseded run on {args.sha}: no visual artifacts had arrived; nothing to publish.")


def main() -> None:
    args = parser().parse_args()
    github_token = os.environ.get("GITHUB_TOKEN")
    if not github_token:
        raise ValueError("GITHUB_TOKEN is required to publish the visual-review check run")
    api_key = _buildbuddy_api_key()
    workflow_url = current_workflow_url()

    conclusion: Literal["success", "failure", "neutral"] = "neutral"
    summary = "No tests executed by Bazel CI exposed visual-review.json."
    comment_body: str | None = None
    refresh_stale_comment_body: str | None = None
    details_url: str | None = args.ci_details_url or workflow_url
    ci_failures: list[str] = []

    if args.ci_conclusion == "cancelled":
        # `bazel-ci.yml` cancels a superseded run the instant a newer commit lands on
        # the branch. Such a run still has nothing true to *say* — the comment is a
        # singleton, so letting it speak would overwrite the previous run's real review
        # with a warning about a build nobody wants, and the run that superseded it is
        # already on its way with the answer.
        #
        # It does, however, often have artifacts. Cancellation kills the workflow, not
        # the Bazel invocation, which frequently completed with a full set of manifests
        # already streamed to BuildBuddy. Those are published: the commit bundle is
        # immutable and additive, and it is what lets a PR based on this commit resolve
        # an exact baseline instead of falling back to a pointer.
        publish_only(args, github_token=github_token, api_key=api_key, details_url=details_url)
        return

    try:
        invocations = _invocations_for(args, api_key=api_key)
        if args.ci_conclusion != "success":
            ci_failures = list_ci_failures(invocations)
        tests = download_visual_tests(invocations, args.work_dir / "tests", api_key=api_key)
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
            #
            # Reachable only for a run that was not cancelled, so the invocation
            # behind `tests` has necessarily finished. That is what keeps a
            # superseded run's half-streamed read (README.md § Gotcha: a
            # superseded run's publish races its own Bazel invocation) able to
            # leave a pointer stale but never to move it somewhere wrong.
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
