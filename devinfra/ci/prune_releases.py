# /// script
# requires-python = ">=3.12"
# dependencies = ["PyGithub>=1.77", "pydantic>=2.0", "httpx>=0.27", "more-itertools>=10.0"]
# ///
"""Delete GitHub Releases past the retention window, once per day for the whole repo.

This used to run inside every release-matrix job. `gh release list` is the only
GraphQL call in the release path, it pages the entire release list, and it ran in
~66 concurrent jobs that each threw away every package but their own -- which
exhausted the 5,000/hour GraphQL quota and failed the release lane wholesale.
One pass, once a day, over REST costs a few hundredths of that.

Expects: GH_TOKEN env var.
"""

import os
import sys
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

# Add repo root to path for devinfra.ci imports when running via uv
_REPO_ROOT = Path(__file__).parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from github import Auth, Github
from pydantic import BaseModel, Field

from devinfra.ci.artifacts import Sources, sources_path, tag_package

REPO = "agentydragon/ducktape"
RETENTION_DAYS = 30


class Release(BaseModel, frozen=True):
    """The fields of a GitHub Release this decision needs."""

    tag: str
    created_at: datetime = Field(description="Timezone-aware; compared against the cutoff")


def pinned_tags(sources: Sources) -> set[str]:
    """Tags that nix/artifact-pins.json currently fetches from.

    Deleting one of these 404s the nix build, so they are protected regardless of age.
    """
    prefix = f"https://github.com/{REPO}/releases/download/"
    return {pin.url.removeprefix(prefix).split("/")[0] for pin in sources.pins.values() if pin.url.startswith(prefix)}


def releases_to_delete(releases: list[Release], *, pinned: set[str], cutoff: datetime) -> list[str]:
    """Tags older than the cutoff, minus the ones that must survive.

    Two protections, both load-bearing:

    - **Pinned tags**, because nix fetches them by URL.
    - **The newest release of each package**, because a package whose content has not
      changed publishes nothing new -- the release-artifact skip check sees its tag
      already exists -- so its live release ages past the cutoff while still being the
      current one. This is what the old per-job prune's `KEEP_TAG` protected.
    """
    newest: dict[str, Release] = {}
    for release in releases:
        pkg = tag_package(release.tag)
        if pkg is None:
            continue
        if pkg not in newest or release.created_at > newest[pkg].created_at:
            newest[pkg] = release

    keep = pinned | {release.tag for release in newest.values()}
    return [
        release.tag
        for release in releases
        if tag_package(release.tag) is not None and release.created_at < cutoff and release.tag not in keep
    ]


def main() -> None:
    repo = Github(auth=Auth.Token(os.environ["GH_TOKEN"])).get_repo(REPO)
    releases = [
        Release(tag=release.tag_name, created_at=release.created_at)
        for release in repo.get_releases()
        if not release.draft and not release.prerelease
    ]
    pinned = pinned_tags(Sources.model_validate_json(sources_path().read_text()))
    stale = releases_to_delete(releases, pinned=pinned, cutoff=datetime.now(UTC) - timedelta(days=RETENTION_DAYS))

    by_package: dict[str, int] = defaultdict(int)
    for tag in stale:
        by_package[tag_package(tag) or "?"] += 1
    print(f"{len(releases)} releases, {len(pinned)} pinned, deleting {len(stale)}")
    for pkg, count in sorted(by_package.items()):
        print(f"  {pkg}: {count}")

    for release in repo.get_releases():
        if release.tag_name in stale:
            print(f"deleting {release.tag_name}")
            release.delete_release()
            repo.get_git_ref(f"tags/{release.tag_name}").delete()


if __name__ == "__main__":
    main()
