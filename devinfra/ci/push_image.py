"""Publish an OCI layout if its locally built digest differs from the published digest.

The planner can schedule this job before it knows the digest. Rechecking here avoids
minting a fresh tag that would trigger a Flux commit and rollout for identical bytes.
"""

from __future__ import annotations

import argparse
import datetime
import os
from pathlib import Path

from devinfra.ci.image_registry import DIGEST_SUFFIX, Registry, newest_pinned_digest, registry_digest, repo_for
from devinfra.oci.crane import Crane


def pinned_tag(now: datetime.datetime, commit: str, *, prefix: str = "devel") -> str:
    """Mint a tag Flux can select and order newest-alphabetically."""
    return f"{prefix}-{now:%Y%m%d%H%M%S}-{commit[:7]}"


def push(
    oci_dir: Path,
    repo: str,
    tag: str,
    crane: Crane,
    *,
    newest_pinned_prefix: str | None = None,
    check_only: bool = False,
) -> bool:
    """Publish `oci_dir` as `repo:tag` unless the registry already holds that content.

    Check-only mode returns the same decision without writing tags. An unreadable
    registry raises before either write.
    """
    local = oci_dir.with_name(oci_dir.name + DIGEST_SUFFIX).read_text().strip()
    digest = (
        newest_pinned_digest(crane, repo, newest_pinned_prefix)
        if newest_pinned_prefix is not None
        else registry_digest(crane, repo)
    )
    if digest == local:
        print(f"{repo}: {local} is already published; not pushing")
        return False
    if check_only:
        return True
    print(f"{repo}: pushing {local} as :{tag}")
    crane.push(oci_dir, f"{repo}:{tag}")
    crane.tag(f"{repo}:{tag}", "latest")
    return True


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--name", required=True, help="image name, as in image_targets.json")
    parser.add_argument("--registry", required=True, type=Registry, choices=list(Registry))
    parser.add_argument("--oci-dir", required=True, type=Path, help="the built OCI layout directory")
    parser.add_argument("--tag-prefix", default="devel")
    parser.add_argument("--newest-pinned", action="store_true", help="compare the newest pinned branch tag")
    parser.add_argument("--check-only", action="store_true", help="write the changed verdict to GITHUB_OUTPUT")
    args = parser.parse_args(argv)

    changed = push(
        args.oci_dir,
        repo_for(args.name, args.registry),
        pinned_tag(datetime.datetime.now(datetime.UTC), os.environ["GITHUB_SHA"], prefix=args.tag_prefix),
        Crane(),
        newest_pinned_prefix=args.tag_prefix if args.newest_pinned else None,
        check_only=args.check_only,
    )
    if args.check_only:
        with Path(os.environ["GITHUB_OUTPUT"]).open("a", encoding="utf-8") as output:
            print(f"push={str(changed).lower()}", file=output)


if __name__ == "__main__":
    main()
