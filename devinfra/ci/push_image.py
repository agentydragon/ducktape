"""Publish one image, unless the registry already holds exactly these bytes.

The planner it fans out from fails open: an image whose digest `bazel-ci` never
reported, whose digest blob could not be read, or whose invocation ids were empty
lands in the push matrix on the grounds that "unproven" is not "unchanged". That
verdict is a guess, and this job is where it gets checked — it has built the image
itself, so it holds the digest the planner lacked.

Pushing anyway is not free and not idempotent. Every push mints a fresh
`devel-<timestamp>-<sha>` tag, Flux ImagePolicy picks the newest one, and its
image-automation controller commits the new tag back to this repository — so an
image that did not change still costs a commit, a reconcile and a rollout. On devel
that happened once per merge for `manifold-mcp-server`, whose target lives in a
nested module and so is absent from the `//...` sweep the planner reads.

Comparing here rather than teaching the planner to see that target keeps the check
in one place and covers the other fail-open reasons too, which no amount of build
coverage can remove.
"""

from __future__ import annotations

import argparse
import datetime
import os
import sys
from pathlib import Path

from devinfra.ci.image_registry import DIGEST_SUFFIX, Registry, published_digest, repo_for
from util.crane import Crane


def pinned_tag(now: datetime.datetime, commit: str) -> str:
    """The tag shape Flux ImagePolicy filters on and orders newest-alphabetical.

    The only place that convention lives in this repository, and it has to live
    somewhere: minting a tag means knowing the format. Nothing *reads* tags back
    through it — the publish check goes through `latest`, so the cluster's ordering
    rule is not restated here as a parser.
    """
    return f"devel-{now:%Y%m%d%H%M%S}-{commit[:7]}"


def push(oci_dir: Path, repo: str, tag: str, crane: Crane) -> bool:
    """Publish `oci_dir` as `repo:tag` unless that content is already the newest tag.

    Returns whether anything was pushed.
    """
    local = oci_dir.with_name(oci_dir.name + DIGEST_SUFFIX).read_text().strip()
    try:
        digest = published_digest(crane, repo)
    except RuntimeError as e:
        # Same rule as the planner's: unreadable is not unchanged. Reading is a
        # saving, not a precondition — a registry blip must not turn a push that
        # would have worked into a failed job.
        print(f"::warning::{repo}: could not read the published digest ({e}); pushing anyway", file=sys.stderr)
        digest = None
    if digest == local:
        print(f"{repo}: {local} is already published; not pushing")
        return False
    print(f"{repo}: pushing {local} as :{tag}")
    crane.push(oci_dir, f"{repo}:{tag}")
    crane.tag(f"{repo}:{tag}", "latest")
    return True


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--name", required=True, help="image name, as in image_targets.json")
    parser.add_argument("--registry", required=True, type=Registry, choices=list(Registry))
    parser.add_argument("--oci-dir", required=True, type=Path, help="the built OCI layout directory")
    args = parser.parse_args(argv)

    push(
        args.oci_dir,
        repo_for(args.name, args.registry),
        pinned_tag(datetime.datetime.now(datetime.UTC), os.environ["GITHUB_SHA"]),
        Crane(),
    )


if __name__ == "__main__":
    main()
