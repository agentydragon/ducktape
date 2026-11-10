# /// script
# requires-python = ">=3.12"
# dependencies = ["PyGithub>=1.77", "pydantic>=2.0", "httpx>=0.27"]
# ///
"""Sync nix/artifact-pins.json with the latest GitHub Release for each package.

For each pinned package, finds the latest release tag, compares the URL
against the current pin, and updates nix/artifact-pins.json if the pin is stale.

Expects: GH_TOKEN env var.
"""

import io
import os
import sys
import zipfile
from pathlib import Path
from typing import Any

# Add repo root to path for devinfra.ci imports when running via uv
_REPO_ROOT = Path(__file__).parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import httpx
from github import Auth, Github
from github.GithubException import GithubException

from devinfra.ci.artifacts import ARTIFACTS, Pin, Sources, is_tag_for_pkg, sources_path, url_sha256

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


def release_is_on_devel(repo: Any, release: Any, devel_sha: str, reachable: dict[str, bool]) -> bool:
    """Whether a release was built from a commit reachable from current devel.

    Release workflows can be manually dispatched from any ref.  A release from a
    feature branch must not become the repository-wide pin merely because it is
    newer than the last release from devel.
    """
    target = release.target_commitish
    if not target:
        return False
    if target not in reachable:
        try:
            comparison = repo.compare(target, devel_sha)
        except GithubException as error:
            print(f"{release.tag_name}: cannot compare {target} with devel: {error}")
            reachable[target] = False
        else:
            reachable[target] = comparison.status in {"ahead", "identical"}
    return reachable[target]


def main() -> None:
    gh_token = os.environ["GH_TOKEN"]
    repo = Github(auth=Auth.Token(gh_token)).get_repo(REPO)

    # Sort newest-first — GitHub's REST API does not guarantee chronological order.
    # Scan the full list, not a newest-N window: with ~40 per-skill families
    # (skill-*), a rarely-changed skill's latest release can sit far down the
    # list, and truncating would silently freeze (or never create) its pin.
    releases = sorted(
        (r for r in repo.get_releases() if not r.draft and not r.prerelease), key=lambda r: r.created_at, reverse=True
    )
    devel_sha = repo.get_branch("devel").commit.sha
    reachable: dict[str, bool] = {}

    sources = Sources.model_validate_json(sources_path().read_text())

    releases_by_package = {
        artifact.pkg: next(
            (
                r
                for r in releases
                if is_tag_for_pkg(r.tag_name, artifact.release_tag_prefix)
                and release_is_on_devel(repo, r, devel_sha, reachable)
            ),
            None,
        )
        for artifact in ARTIFACTS
    }
    artifacts_by_package = {artifact.pkg: artifact for artifact in ARTIFACTS}

    updated = []
    for artifact in ARTIFACTS:
        release = releases_by_package[artifact.pkg]
        if not release:
            print(f"{artifact.pkg}: no release from devel found, skipping")
            continue
        tag = release.tag_name
        url = f"{BASE}/{tag}/{artifact.filename}"
        pin = sources.pins.get(artifact.pkg)
        if pin and url == pin.url:
            print(f"{artifact.pkg}: up to date ({tag})")
            continue

        if artifact.pkg == "ducktape-util":
            consumers: dict[str, bytes] = {}
            compatible = True
            for name in ("ws", "gmail-archiver"):
                candidate = releases_by_package[name]
                if candidate:
                    filename = artifacts_by_package[name].filename
                    consumer_url = f"{BASE}/{candidate.tag_name}/{filename}"
                elif current := sources.pins.get(name):
                    consumer_url = current.url
                else:
                    print(f"{artifact.pkg}: no {name} wheel available; keeping the existing pin")
                    compatible = False
                    break
                consumers[name] = download_wheel(consumer_url)
            else:
                missing = missing_wheel_imports(download_wheel(url), consumers)
                if missing:
                    print(f"{artifact.pkg}: keeping the existing pin; {', '.join(missing)}")
                    compatible = False
            if not compatible:
                continue

        print(f"{artifact.pkg}: updating to {tag}")
        new_hash = url_sha256(url)
        if pin is None:
            sources.pins[artifact.pkg] = Pin(url=url, sha256=new_hash)
        else:
            pin.url = url
            pin.sha256 = new_hash
        print(f"{artifact.pkg}: {new_hash}")
        updated.append(artifact.pkg)

    if not updated:
        print("All pins up to date")
        return

    sources_path().write_text(sources.model_dump_json(indent=2) + "\n")
    print(f"Updated: {' '.join(updated)}")

    # Write updated package names for use in commit message
    updated_file = Path(os.environ.get("GITHUB_OUTPUT", "/dev/null"))
    with updated_file.open("a") as f:
        f.write(f"updated={', '.join(updated)}\n")


if __name__ == "__main__":
    main()
