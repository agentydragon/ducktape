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

from jinja2 import Environment, FileSystemLoader, select_autoescape

FULL_SHA = re.compile(r"^[0-9a-f]{40}$")
COMPONENT = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
Runner = Callable[..., subprocess.CompletedProcess[str]]


def find_test_invocation(linkage_dir: Path) -> str:
    for path in sorted(linkage_dir.glob("*.json")):
        record = json.loads(path.read_text())
        for invocation in record.get("buildbuddy", {}).get("bazel_invocations", []):
            if invocation.get("role") == "test" and invocation.get("invocation_id"):
                return str(invocation["invocation_id"])
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
    manifest = json.loads(manifest_path.read_text())
    expected = manifest.get("screenshots")
    if not isinstance(expected, list) or not expected:
        raise ValueError("visual manifest must contain a non-empty screenshots list")
    if any(not isinstance(name, str) or Path(name).name != name or not name.endswith(".png") for name in expected):
        raise ValueError("visual manifest screenshot names must be safe PNG basenames")
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


def upload_bundle(bundle: Path, *, endpoint: str, bucket: str, key: str, run: Runner = subprocess.run) -> None:
    destination = f"s3://{bucket}/{key.strip('/')}"
    run(
        [
            "aws",
            "--endpoint-url",
            endpoint,
            "s3",
            "cp",
            f"{bundle}/",
            f"{destination}/",
            "--recursive",
            "--exclude",
            "index.html",
            "--cache-control",
            "public,max-age=31536000,immutable",
        ],
        check=True,
        text=True,
    )
    run(
        [
            "aws",
            "--endpoint-url",
            endpoint,
            "s3",
            "cp",
            str(bundle / "index.html"),
            f"{destination}/index.html",
            "--content-type",
            "text/html",
            "--cache-control",
            "public,max-age=31536000,immutable",
        ],
        check=True,
        text=True,
    )


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


if __name__ == "__main__":
    main()
