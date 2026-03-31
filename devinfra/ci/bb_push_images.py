"""Push Bazel-built OCI images to GHCR and tag for Flux.

Builds all OCI image targets in a single bazel build, then uses crane directly
(from runfiles) to push, tag, and compare digests. Only creates a new pinned
tag (branch-YYYYMMDDHHMMSS-sha7) when the image digest actually changed,
preventing spurious Flux repins.
"""

import base64
import json
import os
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from util.bazel import runfiles
from util.env import get_required_env

_CRANE_RLOCATION = "crane/crane"


@dataclass(frozen=True)
class GhcrImage:
    """An OCI image built by Bazel and pushed to GHCR."""

    image_target: str
    repository: str


IMAGES = [
    GhcrImage("//cluster/k8s/inventree/token-provisioner:image", "ghcr.io/agentydragon/token-provisioner"),
    GhcrImage("//props/backend:image", "ghcr.io/agentydragon/props-backend"),
    GhcrImage("//airlock:image", "ghcr.io/agentydragon/airlock"),
    GhcrImage("//airlock/auth_proxy:image", "ghcr.io/agentydragon/auth-proxy"),
    GhcrImage("//mcp_infra/exec:direct_image", "ghcr.io/agentydragon/exec-backend"),
    GhcrImage("//openclaw/exec:image", "ghcr.io/agentydragon/openclaw-exec"),
    GhcrImage("//homeassistant/proxy:image", "ghcr.io/agentydragon/homeassistant-proxy"),
    GhcrImage("//inventree_utils/rai_plugin:image", "ghcr.io/agentydragon/inventree"),
    GhcrImage("//tana/token_broker:image", "ghcr.io/agentydragon/tana-token-broker"),
    GhcrImage("//third_party/activitywatch:image", "ghcr.io/agentydragon/aw-server"),
]


def crane(crane_path: Path, *args: str) -> str:
    """Run crane and return stdout."""
    result = subprocess.run([str(crane_path), *args], check=True, capture_output=True, text=True)
    return result.stdout.strip()


def setup_ghcr_auth(username: str, token: str) -> None:
    """Write ~/.docker/config.json for crane registry auth."""
    docker_dir = Path.home() / ".docker"
    docker_dir.mkdir(exist_ok=True)
    auth = base64.b64encode(f"{username}:{token}".encode()).decode()
    config = {"auths": {"ghcr.io": {"auth": auth}}}
    (docker_dir / "config.json").write_text(json.dumps(config))


def image_output_dir(image_target: str, bazel_bin: Path) -> Path:
    """Convert a Bazel label like //foo/bar:image to bazel-bin/foo/bar/image."""
    label = image_target.lstrip("/")
    pkg, name = label.split(":")
    return bazel_bin / pkg / name


def read_local_digest(image_dir: Path) -> str:
    """Read the image digest from the OCI layout's index.json."""
    index = json.loads((image_dir / "index.json").read_text())
    digest: str = index["manifests"][0]["digest"]
    return digest


def latest_pinned_tag(crane_path: Path, repo: str, branch: str) -> str | None:
    """Find the most recent pinned tag for the given branch, or None."""
    try:
        tags = crane(crane_path, "ls", repo).splitlines()
    except subprocess.CalledProcessError:
        return None
    branch_tags = sorted(t for t in tags if t.startswith(f"{branch}-"))
    return branch_tags[-1] if branch_tags else None


def push_and_tag(img: GhcrImage, crane_path: Path, bazel_bin: Path, pinned_tag: str, branch: str) -> None:
    image_dir = image_output_dir(img.image_target, bazel_bin)
    local_digest = read_local_digest(image_dir)

    # Check if the registry already has this digest before pushing.
    current_tag = latest_pinned_tag(crane_path, img.repository, branch)
    if current_tag:
        current_digest = crane(crane_path, "digest", f"{img.repository}:{current_tag}")
        if local_digest == current_digest:
            print(f"{img.repository}: digest unchanged ({local_digest[:19]}), skipping")
            return

    # Push the OCI layout by digest, then tag.
    print(f"{img.repository}: pushing {local_digest[:19]}")
    crane(crane_path, "push", str(image_dir), f"{img.repository}@{local_digest}")
    crane(crane_path, "tag", f"{img.repository}@{local_digest}", "latest")
    print(f"{img.repository}: tagging {pinned_tag}")
    crane(crane_path, "tag", f"{img.repository}@{local_digest}", pinned_tag)


def main() -> None:
    workspace = os.environ.get("BUILD_WORKSPACE_DIRECTORY")
    if workspace:
        os.chdir(workspace)

    subject = subprocess.check_output(["git", "log", "-1", "--format=%s"], text=True).strip()
    if "[skip ci]" in subject:
        print("Commit message contains [skip ci], skipping image push.")
        return

    setup_ghcr_auth(get_required_env("GHCR_USERNAME"), get_required_env("GHCR_TOKEN"))

    # Build all image targets in one invocation.
    targets = [img.image_target for img in IMAGES]
    print(f"Building {len(targets)} image targets...")
    subprocess.run(["bazel", "build", "--config=rbe", "--remote_download_toplevel", *targets], check=True)

    bazel_bin = Path(subprocess.check_output(["bazel", "info", "bazel-bin"], text=True).strip())
    crane_path = runfiles.get_required_path(_CRANE_RLOCATION)

    branch = subprocess.check_output(["git", "rev-parse", "--abbrev-ref", "HEAD"], text=True).strip()
    ts = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
    sha = subprocess.check_output(["git", "rev-parse", "--short=7", "HEAD"], text=True).strip()
    pinned_tag = f"{branch}-{ts}-{sha}"

    for img in IMAGES:
        push_and_tag(img, crane_path, bazel_bin, pinned_tag, branch)


if __name__ == "__main__":
    main()
