"""Publish immutable, static PR visual-review bundles from trusted CI."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import boto3
from github import Auth, Github
from jinja2 import Environment, FileSystemLoader, select_autoescape
from pydantic import BaseModel, field_validator

FULL_SHA = re.compile(r"^[0-9a-f]{40}$")
COMPONENT = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
Runner = Callable[..., subprocess.CompletedProcess[str]]
COMMENT_MARKER = "<!-- pr-visuals -->"


class BazelInvocation(BaseModel):
    role: str
    invocation_id: str


class BuildBuddyLinkage(BaseModel):
    bazel_invocations: list[BazelInvocation]


class LinkageRecord(BaseModel):
    buildbuddy: BuildBuddyLinkage


class VisualManifest(BaseModel):
    screenshots: list[str]

    @field_validator("screenshots")
    @classmethod
    def validate_screenshots(cls, value: list[str]) -> list[str]:
        if not value:
            raise ValueError("must not be empty")
        if any(Path(name).name != name or not name.endswith(".png") for name in value):
            raise ValueError("screenshot names must be safe PNG basenames")
        return value


def find_test_invocation(linkage_dir: Path) -> str:
    for path in sorted(linkage_dir.glob("*.json")):
        record = LinkageRecord.model_validate_json(path.read_text())
        for invocation in record.buildbuddy.bazel_invocations:
            if invocation.role == "test":
                return invocation.invocation_id
    raise ValueError("no Bazel test invocation found in BuildBuddy linkage")


def download_screenshots(
    invocation: str, manifest_name: str, destination: Path, *, run: Runner = subprocess.run
) -> bool:
    listing = run(["bbapi", "artifact", "list", invocation], check=True, text=True, capture_output=True).stdout
    if manifest_name not in listing:
        return False
    destination.mkdir(parents=True, exist_ok=True)
    manifest_path = destination / manifest_name
    run(["bbapi", "artifact", "download", invocation, manifest_name, "-o", str(manifest_path)], check=True, text=True)
    expected = VisualManifest.model_validate_json(manifest_path.read_text()).screenshots
    missing = [name for name in expected if name not in listing]
    if missing:
        raise ValueError(f"partial screenshot set in BuildBuddy: missing {', '.join(missing)}")
    for name in expected:
        run(["bbapi", "artifact", "download", invocation, name, "-o", str(destination / name)], check=True, text=True)
    return True


def build_bundle(
    screenshot_dir: Path, output_root: Path, *, commit_sha: str, component: str, title: str, repository: str
) -> Path:
    if not FULL_SHA.fullmatch(commit_sha):
        raise ValueError("commit SHA must be the full 40-character lowercase SHA-1")
    if not COMPONENT.fullmatch(component):
        raise ValueError("component must be a lowercase kebab-case slug")
    screenshots = sorted(screenshot_dir.glob("*.png"))
    if not screenshots:
        raise ValueError(f"no PNG screenshots found in {screenshot_dir}")

    bundle = output_root / "commits" / commit_sha / component
    bundle.mkdir(parents=True, exist_ok=True)
    for screenshot in screenshots:
        shutil.copyfile(screenshot, bundle / screenshot.name)
    metadata = {
        "repository": repository,
        "commitSha": commit_sha,
        "component": component,
        "title": title,
        "screenshots": [shot.name for shot in screenshots],
    }
    (bundle / "metadata.json").write_text(json.dumps(metadata, indent=2) + "\n")

    environment = Environment(
        loader=FileSystemLoader(Path(__file__).parent),
        autoescape=select_autoescape(["html", "j2"]),
        keep_trailing_newline=True,
    )
    page = environment.get_template("pr_visuals.html.j2").render(
        title=title,
        repository=repository,
        commit_sha=commit_sha,
        screenshots=[{"name": shot.name, "label": shot.stem.replace("-", " ")} for shot in screenshots],
    )
    (bundle / "index.html").write_text(page)
    return bundle


def upload_bundle(bundle: Path, *, endpoint: str, bucket: str, key: str, client: Any | None = None) -> None:
    s3 = client or boto3.client("s3", endpoint_url=endpoint)
    prefix = key.strip("/")
    cache_control = "public,max-age=31536000,immutable"
    for path in sorted(bundle.iterdir()):
        if path.name == "index.html":
            continue
        content_type = "image/png" if path.suffix == ".png" else "application/json"
        s3.upload_file(
            str(path),
            bucket,
            f"{prefix}/{path.name}",
            ExtraArgs={"CacheControl": cache_control, "ContentType": content_type},
        )
    # Publish the entry point last so readers never observe a partial bundle.
    s3.upload_file(
        str(bundle / "index.html"),
        bucket,
        f"{prefix}/index.html",
        ExtraArgs={"CacheControl": cache_control, "ContentType": "text/html"},
    )


def upsert_pull_request_comment(
    *, repository: str, pull_request: int, commit_sha: str, title: str, url: str, token: str
) -> None:
    body = f"{COMMENT_MARKER}\n{title} for `{commit_sha}`: [open screenshots]({url})"
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


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser()
    result.add_argument("--linkage-dir", type=Path, required=True)
    result.add_argument("--manifest", required=True)
    result.add_argument("--work-dir", type=Path, required=True)
    result.add_argument("--sha", required=True)
    result.add_argument("--component", required=True)
    result.add_argument("--title", required=True)
    result.add_argument("--repository", required=True)
    result.add_argument("--endpoint", required=True)
    result.add_argument("--bucket", required=True)
    result.add_argument("--public-base-url", required=True)
    result.add_argument("--pull-request", type=int, required=True)
    return result


def main() -> None:
    args = parser().parse_args()
    screenshots = args.work_dir / "screenshots"
    found = download_screenshots(find_test_invocation(args.linkage_dir), args.manifest, screenshots)
    github_output = os.environ.get("GITHUB_OUTPUT")
    if github_output:
        with Path(github_output).open("a") as output:
            output.write(f"found={'true' if found else 'false'}\n")
    if not found:
        print("Haku screenshot target was not in the affected test set; skipping publication")
        return
    bundle = build_bundle(
        screenshots,
        args.work_dir / "site",
        commit_sha=args.sha,
        component=args.component,
        title=args.title,
        repository=args.repository,
    )
    upload_bundle(bundle, endpoint=args.endpoint, bucket=args.bucket, key=f"commits/{args.sha}/{args.component}")
    github_token = os.environ.get("GITHUB_TOKEN")
    if not github_token:
        raise ValueError("GITHUB_TOKEN is required to maintain the pull-request comment")
    public_url = f"{args.public_base_url.rstrip('/')}/commits/{args.sha}/{args.component}/index.html"
    upsert_pull_request_comment(
        repository=args.repository,
        pull_request=args.pull_request,
        commit_sha=args.sha,
        title=args.title,
        url=public_url,
        token=github_token,
    )


if __name__ == "__main__":
    main()
