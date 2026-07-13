"""Publish visual-review bundles and devel baseline pointers from Bazel tests run by trusted CI."""

from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import boto3
from github import Auth, Github
from jinja2 import Environment, FileSystemLoader, select_autoescape
from pydantic import BaseModel, Field, TypeAdapter

from util.visual_review import MANIFEST_NAME, VisualReviewManifest

FULL_SHA = re.compile(r"^[0-9a-f]{40}$")
Runner = Callable[..., subprocess.CompletedProcess[str]]
COMMENT_MARKER = "<!-- pr-visuals -->"
BASELINE_POINTER_SCHEMA = "ducktape.visual-baseline.v1"
# Mutable "latest" pointer: no repo precedent for mutable S3 objects, so force
# revalidation and never serve a flipped baseline from an edge cache.
BASELINE_POINTER_CACHE_CONTROL = "no-cache, max-age=0, must-revalidate"


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


class BaselinePointer(BaseModel):
    """Mutable per-test pointer at ``baselines/<slug>/latest.json`` naming the newest successful ``devel`` baseline."""

    schema_: Literal["ducktape.visual-baseline.v1"] = Field(default=BASELINE_POINTER_SCHEMA, alias="schema")
    repository: str
    target_label: str
    commit_sha: str
    asset_paths: list[str]
    url: str


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
        if len(manifests) != 1:
            raise ValueError(f"{target_label} exposed {len(manifests)} visual manifests; expected exactly one")

        slug = target_slug(target_label)
        if previous := used_slugs.get(slug):
            raise ValueError(f"Bazel target URL slug collision: {previous} and {target_label}")
        used_slugs[slug] = target_label
        test_dir = destination / slug
        manifest_path = test_dir / MANIFEST_NAME
        _download_artifact(manifests[0], manifest_path, bbapi=bbapi, run=run)
        manifest = VisualReviewManifest.model_validate_json(manifest_path.read_text())

        available = {artifact.artifact.name: artifact for artifact in target_artifacts}
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


def build_bundle(tests: list[DownloadedVisualTest], output_root: Path, *, commit_sha: str, repository: str) -> Path:
    if not FULL_SHA.fullmatch(commit_sha):
        raise ValueError("commit SHA must be the full 40-character lowercase SHA-1")
    if not tests:
        raise ValueError("cannot build a visual-review bundle without tests")

    bundle = output_root / "commits" / commit_sha
    environment = _templates()
    page_tests: list[dict[str, Any]] = []
    metadata_tests: list[dict[str, Any]] = []
    for test in tests:
        target_dir = bundle / "tests" / test.slug
        target_dir.mkdir(parents=True, exist_ok=True)
        assets = [asset.model_dump() for asset in test.manifest.assets]
        for asset in test.manifest.assets:
            shutil.copyfile(test.directory / asset.path, target_dir / asset.path)
        target_data = {
            "target_label": test.target_label,
            "slug": test.slug,
            "title": test.manifest.title,
            "assets": assets,
        }
        (target_dir / "metadata.json").write_text(json.dumps(target_data, indent=2) + "\n")
        (target_dir / "index.html").write_text(
            environment.get_template("pr_visual_test.html.j2").render(
                repository=repository, commit_sha=commit_sha, **target_data
            )
        )
        page_tests.append(target_data)
        metadata_tests.append(target_data)

    bundle.mkdir(parents=True, exist_ok=True)
    metadata = {"repository": repository, "commitSha": commit_sha, "tests": metadata_tests}
    (bundle / "metadata.json").write_text(json.dumps(metadata, indent=2) + "\n")
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


def publish_baselines(
    tests: list[DownloadedVisualTest],
    *,
    repository: str,
    commit_sha: str,
    endpoint: str,
    bucket: str,
    public_base_url: str,
    work_dir: Path,
    client: Any | None = None,
) -> list[str]:
    if not FULL_SHA.fullmatch(commit_sha):
        raise ValueError("commit SHA must be the full 40-character lowercase SHA-1")
    if not tests:
        raise ValueError("cannot publish baselines without tests")

    # Cross-commit staleness guard (a late run overwriting a newer pointer) is
    # deferred to the plan's hardening phase; the pointer stays content-addressed.
    s3 = client or boto3.client("s3", endpoint_url=endpoint)
    base_url = public_base_url.rstrip("/")
    staging = work_dir / "baselines"
    keys: list[str] = []
    for test in tests:
        pointer = BaselinePointer(
            repository=repository,
            target_label=test.target_label,
            commit_sha=commit_sha,
            asset_paths=[asset.path for asset in test.manifest.assets],
            url=f"{base_url}/commits/{commit_sha}/tests/{test.slug}/",
        )
        pointer_path = staging / test.slug / "latest.json"
        pointer_path.parent.mkdir(parents=True, exist_ok=True)
        pointer_path.write_text(json.dumps(pointer.model_dump(by_alias=True), indent=2) + "\n")
        key = f"baselines/{test.slug}/latest.json"
        s3.upload_file(
            pointer_path,
            bucket,
            key,
            ExtraArgs={"CacheControl": BASELINE_POINTER_CACHE_CONTROL, "ContentType": "application/json"},
        )
        keys.append(key)
    return keys


def success_comment_body(*, repository: str, commit_sha: str, url: str, tests: list[DownloadedVisualTest]) -> str:
    commit_url = f"https://github.com/{repository}/commit/{commit_sha}"
    page_url = f"{url}index.html"
    lines = [
        COMMENT_MARKER,
        f"## Visual review for [`{commit_sha[:8]}`]({commit_url})",
        "",
        f"{len(tests)} Bazel test target{'s' if len(tests) != 1 else ''} produced visual artifacts. "
        f"[Open visual review]({page_url}).",
        "",
    ]
    lines.extend(
        f"- [`{test.target_label}`]({url}tests/{test.slug}/index.html): {len(test.manifest.assets)} assets"
        for test in tests
    )
    return "\n".join(lines)


def error_comment_body(*, repository: str, commit_sha: str, error: Exception) -> str:
    commit_url = f"https://github.com/{repository}/commit/{commit_sha}"
    message = str(error).replace("```", "'''")[:2000]
    return "\n".join(
        [
            COMMENT_MARKER,
            f"## Visual review failed for [`{commit_sha[:8]}`]({commit_url})",
            "",
            "> [!CAUTION]",
            "> Bazel CI produced an invalid or incomplete visual-review artifact set.",
            "",
            "```text",
            message,
            "```",
        ]
    )


def upsert_pull_request_comment(*, repository: str, pull_request: int, body: str, token: str) -> None:
    with Github(auth=Auth.Token(token)) as github:
        issue = github.get_repo(repository).get_issue(pull_request)
        existing = next(
            (
                comment
                for comment in issue.get_comments()
                if comment.user is not None and comment.user.type == "Bot" and COMMENT_MARKER in (comment.body or "")
            ),
            None,
        )
        if existing is None:
            issue.create_comment(body)
        else:
            existing.edit(body)


def publish_check_run(
    *,
    repository: str,
    commit_sha: str,
    conclusion: Literal["success", "failure", "neutral"],
    summary: str,
    details_url: str | None,
    token: str,
) -> None:
    with Github(auth=Auth.Token(token)) as github:
        repo = github.get_repo(repository)
        output: dict[str, str | list[dict[str, str | int]]] = {"title": "PR visual review", "summary": summary[:65000]}
        if details_url is None:
            repo.create_check_run(
                name="PR visual review", head_sha=commit_sha, status="completed", conclusion=conclusion, output=output
            )
        else:
            repo.create_check_run(
                name="PR visual review",
                head_sha=commit_sha,
                status="completed",
                conclusion=conclusion,
                details_url=details_url,
                output=output,
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
    result.add_argument("--linkage-dir", type=Path, required=True)
    result.add_argument("--work-dir", type=Path, required=True)
    result.add_argument("--sha", required=True)
    result.add_argument("--repository", required=True)
    result.add_argument("--endpoint", required=True)
    result.add_argument("--bucket", required=True)
    result.add_argument("--public-base-url", required=True)
    result.add_argument("--pull-request", type=int)
    result.add_argument("--update-baselines", action="store_true")
    return result


def main() -> None:
    args = parser().parse_args()
    github_token = os.environ.get("GITHUB_TOKEN")
    if not github_token:
        raise ValueError("GITHUB_TOKEN is required to publish the visual-review check run")
    workflow_url = current_workflow_url()

    try:
        tests = download_visual_tests(find_test_invocations(args.linkage_dir), args.work_dir / "tests")
        github_output = os.environ.get("GITHUB_OUTPUT")
        if github_output:
            with Path(github_output).open("a") as output:
                output.write(f"found={'true' if tests else 'false'}\n")
        if not tests:
            summary = "No tests executed by Bazel CI exposed visual-review.json."
            print(f"{summary} Skipping publication.")
            publish_check_run(
                repository=args.repository,
                commit_sha=args.sha,
                conclusion="neutral",
                summary=summary,
                details_url=workflow_url,
                token=github_token,
            )
            return

        bundle = build_bundle(tests, args.work_dir / "site", commit_sha=args.sha, repository=args.repository)
        upload_bundle(bundle, endpoint=args.endpoint, bucket=args.bucket, key=f"commits/{args.sha}")
        public_url = f"{args.public_base_url.rstrip('/')}/commits/{args.sha}/"
        public_page_url = f"{public_url}index.html"
        summary = f"{len(tests)} Bazel test target{'s' if len(tests) != 1 else ''} produced visual artifacts."
        if args.update_baselines:
            publish_baselines(
                tests,
                repository=args.repository,
                commit_sha=args.sha,
                endpoint=args.endpoint,
                bucket=args.bucket,
                public_base_url=args.public_base_url,
                work_dir=args.work_dir,
            )
            summary += f" Updated {len(tests)} devel baseline pointer{'s' if len(tests) != 1 else ''}."
        if args.pull_request is not None:
            upsert_pull_request_comment(
                repository=args.repository,
                pull_request=args.pull_request,
                body=success_comment_body(repository=args.repository, commit_sha=args.sha, url=public_url, tests=tests),
                token=github_token,
            )
        publish_check_run(
            repository=args.repository,
            commit_sha=args.sha,
            conclusion="success",
            summary=summary,
            details_url=public_page_url,
            token=github_token,
        )
    except Exception as error:
        try:
            if args.pull_request is not None:
                upsert_pull_request_comment(
                    repository=args.repository,
                    pull_request=args.pull_request,
                    body=error_comment_body(repository=args.repository, commit_sha=args.sha, error=error),
                    token=github_token,
                )
            publish_check_run(
                repository=args.repository,
                commit_sha=args.sha,
                conclusion="failure",
                summary=str(error),
                details_url=workflow_url,
                token=github_token,
            )
        except Exception as reporting_error:
            print(f"Failed to report visual-review error to GitHub: {reporting_error}", file=sys.stderr)
        raise


if __name__ == "__main__":
    main()
