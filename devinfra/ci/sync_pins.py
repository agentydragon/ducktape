"""Sync npins/sources.json with the latest GitHub Release for each package.

For each pinned package, finds the latest release tag, compares the URL
against the current pin, and updates npins/sources.json if the pin is stale.

Expects: GH_TOKEN env var. For fetch=unpack artifacts, requires nix in PATH.
"""

import os
import subprocess

from github import Auth, Github

from devinfra.ci.artifacts import ARTIFACTS, SOURCES_PATH, Sources, url_sha256

REPO = "agentydragon/ducktape"
BASE = f"https://github.com/{REPO}/releases/download"


def nix_unpack_hash(url: str) -> str:
    """Return the NAR hash for a tarball URL (for fetch=unpack pins in flake.nix)."""
    raw = subprocess.run(
        ["nix-prefetch-url", "--unpack", url], capture_output=True, text=True, check=True
    ).stdout.strip()
    return subprocess.run(
        ["nix", "hash", "to-sri", f"sha256:{raw}"], capture_output=True, text=True, check=True
    ).stdout.strip()


def fetch_hash(url: str, fetch: str) -> str:
    if fetch == "unpack":
        return nix_unpack_hash(url)
    return url_sha256(url)


def main() -> None:
    gh_token = os.environ["GH_TOKEN"]
    repo = Github(auth=Auth.Token(gh_token)).get_repo(REPO)

    all_tags = [r.tag_name for r in repo.get_releases() if not r.draft and not r.prerelease][:200]

    sources = Sources.model_validate_json(SOURCES_PATH.read_text())

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
        new_hash = fetch_hash(url, pin.fetch)
        pin.url = url
        pin.hash = new_hash
        print(f"{artifact.pkg}: {new_hash}")
        updated.append(artifact.pkg)

    if not updated:
        print("All pins up to date")
        return

    SOURCES_PATH.write_text(sources.model_dump_json(indent=2) + "\n")
    print(f"Updated: {' '.join(updated)}")


if __name__ == "__main__":
    main()
