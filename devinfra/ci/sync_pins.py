# /// script
# requires-python = ">=3.12"
# dependencies = ["PyGithub>=1.77", "pydantic>=2.0", "httpx>=0.27"]
# ///
"""Sync pins to already-published content identified by exact-source devel CI.

Expects GH_TOKEN, BUILDBUDDY_API_KEY, and the pinned bbapi on PATH. Missing
evidence retains pins; release creation time never determines desired content.
"""

import argparse
import base64
import io
import os
import sys
import zipfile
from pathlib import Path

# Add repo root to path for devinfra.ci imports when running via uv
_REPO_ROOT = Path(__file__).parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import httpx
from github import Auth, Github
from github.GithubException import GithubException

from devinfra.ci.artifacts import (
    ARTIFACTS,
    Pin,
    Sources,
    artifact_targets_path,
    skills_registry_path,
    sources_path,
    url_sha256,
)
from devinfra.ci.pin_publication import current_run, desired_tags, read_build
from devinfra.ci.plan_releases import load_releases
from devinfra.ci.publication_test_verdicts import effective_rbe_image

# gazelle:include_dep @pypi//httpx

REPO = "agentydragon/ducktape"
BASE = f"https://github.com/{REPO}/releases/download"


def missing_wheel_imports(util_wheel: bytes, consumers: dict[str, bytes]) -> list[str]:
    """Keep old consumers paired with the utility modules their released wheels import."""
    with zipfile.ZipFile(io.BytesIO(util_wheel)) as wheel:
        available = set(wheel.namelist())

    # CLEANUP(added 2026-11-03): remove once the pinned ws and gmail-archiver
    # wheels no longer import util.agent_sandbox and util.fmt respectively.
    uses = {
        "ws": ("util.agent_sandbox", ("devinfra/ws/cli.py",)),
        "gmail-archiver": ("util.fmt", ("gmail_archiver/main.py", "gmail_archiver/planners/doordash.py")),
    }
    missing = []
    for name, (module, paths) in uses.items():
        if module.replace(".", "/") + ".py" in available:
            continue
        with zipfile.ZipFile(io.BytesIO(consumers[name])) as wheel:
            if any(module.encode() in wheel.read(path) for path in paths):
                missing.append(f"{name} imports {module}")
    return missing


def download_wheel(url: str) -> bytes:
    response = httpx.get(url, follow_redirects=True, timeout=30.0)
    response.raise_for_status()
    return response.content


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source-commit", required=True, help="checked-out devel SHA (dry runs may inspect historical devel CI)"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="verify candidates and print changes without writing pins"
    )
    args = parser.parse_args(argv)
    gh_token = os.environ["GH_TOKEN"]
    repo = Github(auth=Auth.Token(gh_token)).get_repo(REPO)
    devel_sha = repo.get_branch("devel").commit.sha
    if args.source_commit != devel_sha:
        if not args.dry_run:
            print("Devel advanced since checkout; retaining pins until the next sync")
            return
        devel_sha = args.source_commit
        print("Historical dry run; no pin changes will be written")
    run = current_run(repo.get_workflow("ci.yml").get_runs(branch="devel", event="push", head_sha=devel_sha), devel_sha)
    rbe_image = effective_rbe_image("")
    if run is None or rbe_image is None:
        print("Current devel CI is pending or unavailable; retaining pins")
        return
    releases = load_releases(artifact_targets_path(), skills_registry_path())
    tags = desired_tags(
        releases,
        source_commit=devel_sha,
        run_id=str(run.id),
        attempt=str(run.run_attempt),
        rbe_image=rbe_image,
        successful_jobs={
            job.name
            for job in run.jobs(_filter="latest")
            if job.status == "completed" and job.conclusion == "success" and job.run_attempt == run.run_attempt
        },
        read=read_build,
    )
    sources = Sources.model_validate_json(sources_path().read_text())
    candidates: dict[str, Pin] = {}
    for release in releases:
        if not (tag := tags.get(release.pkg)):
            print(f"{release.pkg}: no verified build identity; retaining pins")
            continue
        owned = [artifact for artifact in ARTIFACTS if artifact.release_tag_prefix == release.pkg]
        if not owned:
            continue
        try:
            published = repo.get_release(tag)
            if published.tag_name != tag or published.draft or published.prerelease:
                raise ValueError("release is not the selected published content")
            assets = {asset["name"]: asset for asset in published.raw_data["assets"]}
            group: dict[str, Pin] = {}
            for artifact in owned:
                asset = assets.get(artifact.filename)
                stored_digest = asset.get("digest") if asset else None
                if not isinstance(stored_digest, str) or not stored_digest.startswith("sha256:"):
                    raise ValueError("release asset has no SHA-256 digest")
                expected = bytes.fromhex(stored_digest.removeprefix("sha256:"))
                if len(expected) != 32:
                    raise ValueError("release asset has an invalid SHA-256 digest")
                url = f"{BASE}/{tag}/{artifact.filename}"
                current = sources.pins.get(artifact.pkg)
                value = current.sha256 if current and current.url == url else url_sha256(url)
                if base64.b64decode(value, validate=True) != expected:
                    raise ValueError("release asset SHA-256 does not match downloaded or pinned bytes")
                group[artifact.pkg] = Pin(url=url, sha256=value)
            candidates.update(group)
        except (GithubException, httpx.HTTPError, ValueError, KeyError, TypeError) as error:
            print(f"{release.pkg}: release unavailable or unverifiable ({type(error).__name__}); retaining group pins")

    if (candidate_util := candidates.get("ducktape-util")) and candidate_util != sources.pins.get("ducktape-util"):
        consumers: dict[str, bytes] = {}
        try:
            for name in ("ws", "gmail-archiver"):
                pin = candidates.get(name) or sources.pins.get(name)
                if pin is None:
                    raise ValueError(f"no {name} wheel available")
                consumers[name] = download_wheel(pin.url)
            missing = missing_wheel_imports(download_wheel(candidate_util.url), consumers)
            if missing:
                raise ValueError(", ".join(missing))
        except (httpx.HTTPError, ValueError, KeyError, zipfile.BadZipFile) as error:
            print(f"ducktape-util: retaining pin; incompatible or unavailable consumers ({error})")
            del candidates["ducktape-util"]

    updated = [pkg for pkg, pin in candidates.items() if pin != sources.pins.get(pkg)]
    if not updated:
        print("All pins up to date")
        return
    if args.dry_run:
        for pkg in updated:
            print(f"Would update {pkg}: {candidates[pkg].url}")
        return
    sources.pins.update(candidates)
    sources_path().write_text(sources.model_dump_json(indent=2) + "\n")
    print(f"Updated: {' '.join(updated)}")

    # Write updated package names for use in commit message
    updated_file = Path(os.environ.get("GITHUB_OUTPUT", "/dev/null"))
    with updated_file.open("a") as f:
        f.write(f"updated={', '.join(updated)}\n")


if __name__ == "__main__":
    main()
