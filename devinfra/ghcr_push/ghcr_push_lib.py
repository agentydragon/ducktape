"""Push an OCI image to GHCR with conditional tagging.

Uses crane to compare local vs remote digests before pushing. Only creates a
new pinned tag (branch-YYYYMMDDHHMMSS-sha7) when the image digest actually
changed, preventing spurious Flux repins.

Authenticates to GHCR with the workflow-scoped `secrets.GITHUB_TOKEN` forwarded
through `bb remote`'s `x-buildbuddy-platform.env-overrides` into the runner
VM's environment. Because the token is issued by GitHub Actions for the
current workflow run, new packages created by a push are automatically linked
to the source repository and inherit its visibility — for a public repo like
this one, the package is created public without any follow-up API call.
"""

import argparse
import logging
import os
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from util.bazel.runfiles import get_required_path
from util.bazel.workspace import BazelLabel, get_build_workspace_directory
from util.crane import Crane
from util.env import get_required_env
from util.oci import read_oci_layout_digest

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class GhcrImage:
    image_target: str
    repository: str


def _git(*args: str) -> str:
    return subprocess.check_output(["git", *args], text=True).strip()


def _image_runfiles_dir(image_target: str) -> Path:
    """Resolve the OCI layout directory from runfiles."""
    label = BazelLabel.parse(image_target)
    return get_required_path(f"_main/{label.package}/{label.name}")


class ImagePusher:
    def __init__(self, crane: Crane, branch: str, pinned_tag: str) -> None:
        self.crane = crane
        self.branch = branch
        self.pinned_tag = pinned_tag

    def _latest_pinned_tag(self, repo: str) -> str | None:
        try:
            tags = self.crane.ls(repo)
        except subprocess.CalledProcessError:
            return None
        except RuntimeError as exc:
            # `util.crane.Crane._run` wraps subprocess errors in RuntimeError,
            # so first-push bootstrap (`NAME_UNKNOWN` from GHCR because the
            # repository doesn't exist yet) no longer surfaces as
            # CalledProcessError here. Treat it as "no previous tags" so
            # brand-new images can land their first push.
            if "NAME_UNKNOWN" not in str(exc):
                raise
            return None
        branch_tags = sorted(t for t in tags if t.startswith(f"{self.branch}-"))
        return branch_tags[-1] if branch_tags else None

    def push_and_tag(self, img: GhcrImage) -> None:
        image_dir = _image_runfiles_dir(img.image_target)
        local_digest = read_oci_layout_digest(image_dir)
        ref = f"{img.repository}@{local_digest}"

        current_tag = self._latest_pinned_tag(img.repository)
        if current_tag and local_digest == self.crane.digest(f"{img.repository}:{current_tag}"):
            print(f"{img.repository}: digest unchanged ({local_digest[:19]}), skipping")
            return

        print(f"{img.repository}: pushing {local_digest[:19]}")
        self.crane.push(image_dir, ref)
        self.crane.tag(ref, "latest")
        print(f"{img.repository}: tagging {self.pinned_tag}")
        self.crane.tag(ref, self.pinned_tag)


def main() -> None:
    """Push a single OCI image to GHCR if its digest changed."""
    parser = argparse.ArgumentParser(description="Push OCI image to GHCR")
    parser.add_argument("--image-target", required=True)
    parser.add_argument("--repository", required=True)
    args = parser.parse_args()

    os.chdir(get_build_workspace_directory())

    if "[skip ci]" in _git("log", "-1", "--format=%s"):
        print("Commit message contains [skip ci], skipping image push.")
        return

    branch = _git("rev-parse", "--abbrev-ref", "HEAD")
    ts = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
    sha = _git("rev-parse", "--short=7", "HEAD")

    # GHCR_USERNAME/GHCR_TOKEN are forwarded by .github/workflows/push-images.yml
    # via `bb remote --remote_run_header=x-buildbuddy-platform.env-overrides=…`.
    # The token is the workflow-scoped secrets.GITHUB_TOKEN; the username is
    # the conventional "x-access-token" that GHCR accepts for workflow tokens.
    pusher = ImagePusher(
        crane=Crane(
            registry="ghcr.io", username=get_required_env("GHCR_USERNAME"), password=get_required_env("GHCR_TOKEN")
        ),
        branch=branch,
        pinned_tag=f"{branch}-{ts}-{sha}",
    )
    pusher.push_and_tag(GhcrImage(image_target=args.image_target, repository=args.repository))


if __name__ == "__main__":
    main()
