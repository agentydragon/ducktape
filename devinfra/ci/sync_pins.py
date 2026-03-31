# /// script
# requires-python = ">=3.12"
# dependencies = ["PyGithub>=1.77", "pydantic>=2.0", "requests>=2.32"]
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

from devinfra.ci.artifacts import ARTIFACTS, Sources, sources_path, url_sha256

REPO = "agentydragon/ducktape"
BASE = f"https://github.com/{REPO}/releases/download"


def main() -> None:
    gh_token = os.environ["GH_TOKEN"]
    repo = Github(auth=Auth.Token(gh_token)).get_repo(REPO)

    all_tags = [r.tag_name for r in repo.get_releases() if not r.draft and not r.prerelease][:200]

    sources = Sources.model_validate_json(sources_path().read_text())

    updated = []
    for artifact in ARTIFACTS:
        tag = next((t for t in all_tags if t.startswith(f"{artifact.pkg}-")), None)
        if not tag:
            print(f"{artifact.pkg}: no release found, skipping")
            continue

        url = f"{BASE}/{tag}/{artifact.filename}"
        pin = sources.pins[artifact.pkg]
        if url == pin.url:
            print(f"{artifact.pkg}: up to date ({tag})")
            continue

        print(f"{artifact.pkg}: updating to {tag}")
        new_hash = url_sha256(url)
        pin.url = url
        pin.sha256 = new_hash
        print(f"{artifact.pkg}: {new_hash}")
        updated.append(artifact.pkg)

    if not updated:
        print("All pins up to date")
        return

    sources_path().write_text(sources.model_dump_json(indent=2) + "\n")
    print(f"Updated: {' '.join(updated)}")


if __name__ == "__main__":
    main()
