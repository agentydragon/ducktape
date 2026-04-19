# /// script
# requires-python = ">=3.12"
# dependencies = ["PyGithub>=1.77", "pydantic>=2.0", "httpx>=0.27"]
# ///
"""Sync npins/sources.json with the latest GitHub Release for each package.

For each pinned package, finds the latest release tag, compares the URL
against the current pin, and updates npins/sources.json if the pin is stale.

Expects: GH_TOKEN env var.
"""

import os
import sys
from pathlib import Path

# Add repo root to path for devinfra.ci imports when running via uv
_REPO_ROOT = Path(__file__).parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from github import Auth, Github

from devinfra.ci.artifacts import ARTIFACTS, Pin, Sources, is_tag_for_pkg, url_sha256
from util.bazel.workspace import get_build_workspace_directory

REPO = "agentydragon/ducktape"
BASE = f"https://github.com/{REPO}/releases/download"


def sources_path() -> Path:
    return get_build_workspace_directory() / "npins" / "sources.json"


def main() -> None:
    gh_token = os.environ["GH_TOKEN"]
    repo = Github(auth=Auth.Token(gh_token)).get_repo(REPO)

    # Sort newest-first — GitHub's REST API does not guarantee chronological order.
    releases = sorted(
        (r for r in repo.get_releases() if not r.draft and not r.prerelease), key=lambda r: r.created_at, reverse=True
    )[:200]

    sources = Sources.model_validate_json(sources_path().read_text())

    updated = []
    for artifact in ARTIFACTS:
        release = next((r for r in releases if is_tag_for_pkg(r.tag_name, artifact.pkg)), None)
        if not release:
            print(f"{artifact.pkg}: no release found, skipping")
            continue
        tag = release.tag_name
        url = f"{BASE}/{tag}/{artifact.filename}"
        pin = sources.pins.get(artifact.pkg)
        if pin and url == pin.url:
            print(f"{artifact.pkg}: up to date ({tag})")
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
