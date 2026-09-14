# /// script
# requires-python = ">=3.12"
# dependencies = ["PyGithub>=1.77", "pydantic>=2.0", "httpx>=0.27"]
# ///
"""Sync nix/artifact-pins.json with the latest GitHub Release for each package.

For each pinned package, finds the latest release tag, compares the URL
against the current pin, and updates nix/artifact-pins.json if the pin is stale.

Expects: GH_TOKEN env var.
"""

import os
import sys
from pathlib import Path
from typing import Any

# Add repo root to path for devinfra.ci imports when running via uv
_REPO_ROOT = Path(__file__).parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from github import Auth, Github
from github.GithubException import GithubException

from devinfra.ci.artifacts import ARTIFACTS, Pin, Sources, is_tag_for_pkg, sources_path, url_sha256

REPO = "agentydragon/ducktape"
BASE = f"https://github.com/{REPO}/releases/download"


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

    updated = []
    for artifact in ARTIFACTS:
        release = next(
            (
                r
                for r in releases
                if is_tag_for_pkg(r.tag_name, artifact.release_tag_prefix)
                and release_is_on_devel(repo, r, devel_sha, reachable)
            ),
            None,
        )
        if not release:
            print(f"{artifact.pkg}: no release from devel found, skipping")
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
