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


@dataclass(frozen=True)
class GhcrImage:
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


def _git(*args: str) -> str:
    return subprocess.check_output(["git", *args], text=True).strip()


def _image_output_dir(image_target: str, bazel_bin: Path) -> Path:
    """Convert //foo/bar:name to bazel-bin/foo/bar/name."""
    label = image_target.lstrip("/")
    pkg, name = label.split(":")
    return bazel_bin / pkg / name


def _read_local_digest(image_dir: Path) -> str:
    index = json.loads((image_dir / "index.json").read_text())
    digest: str = index["manifests"][0]["digest"]
    return digest


class ImagePusher:
    def __init__(self, crane_path: Path, bazel_bin: Path, branch: str, pinned_tag: str) -> None:
        self.crane_path = crane_path
        self.bazel_bin = bazel_bin
        self.branch = branch
        self.pinned_tag = pinned_tag

    def crane(self, *args: str) -> str:
        result = subprocess.run([str(self.crane_path), *args], check=True, capture_output=True, text=True)
        return result.stdout.strip()

    def latest_pinned_tag(self, repo: str) -> str | None:
        try:
            tags = self.crane("ls", repo).splitlines()
        except subprocess.CalledProcessError:
            return None
        branch_tags = sorted(t for t in tags if t.startswith(f"{self.branch}-"))
        return branch_tags[-1] if branch_tags else None

    def push_and_tag(self, img: GhcrImage) -> None:
        image_dir = _image_output_dir(img.image_target, self.bazel_bin)
        local_digest = _read_local_digest(image_dir)
        ref = f"{img.repository}@{local_digest}"

        current_tag = self.latest_pinned_tag(img.repository)
        if current_tag:
            current_digest = self.crane("digest", f"{img.repository}:{current_tag}")
            if local_digest == current_digest:
                print(f"{img.repository}: digest unchanged ({local_digest[:19]}), skipping")
                return

        print(f"{img.repository}: pushing {local_digest[:19]}")
        self.crane("push", str(image_dir), ref)
        self.crane("tag", ref, "latest")
        print(f"{img.repository}: tagging {self.pinned_tag}")
        self.crane("tag", ref, self.pinned_tag)


def main() -> None:
    workspace = os.environ.get("BUILD_WORKSPACE_DIRECTORY")
    if workspace:
        os.chdir(workspace)

    if "[skip ci]" in _git("log", "-1", "--format=%s"):
        print("Commit message contains [skip ci], skipping image push.")
        return

    username, token = get_required_env("GHCR_USERNAME"), get_required_env("GHCR_TOKEN")
    docker_dir = Path.home() / ".docker"
    docker_dir.mkdir(exist_ok=True)
    auth = base64.b64encode(f"{username}:{token}".encode()).decode()
    (docker_dir / "config.json").write_text(json.dumps({"auths": {"ghcr.io": {"auth": auth}}}))

    targets = [img.image_target for img in IMAGES]
    print(f"Building {len(targets)} image targets...")
    subprocess.run(["bazel", "build", "--config=rbe", "--remote_download_toplevel", *targets], check=True)

    bazel_bin = Path(subprocess.check_output(["bazel", "info", "bazel-bin"], text=True).strip())

    branch = _git("rev-parse", "--abbrev-ref", "HEAD")
    ts = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
    sha = _git("rev-parse", "--short=7", "HEAD")

    pusher = ImagePusher(
        crane_path=runfiles.get_required_path("crane/crane"),
        bazel_bin=bazel_bin,
        branch=branch,
        pinned_tag=f"{branch}-{ts}-{sha}",
    )
    for img in IMAGES:
        pusher.push_and_tag(img)


if __name__ == "__main__":
    main()
