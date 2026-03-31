"""Push Bazel-built OCI images to GHCR and tag for Flux.

Only creates a new pinned tag (branch-YYYYMMDDHHMMSS-sha7) when the image
digest actually changed, preventing spurious Flux repins.
"""

import base64
import json
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from util.env import get_required_env

_BAZEL_RUN = ["bazel", "run", "--config=rbe", "--remote_download_toplevel"]

IMAGE_TARGETS: list[tuple[str, str]] = [
    ("//cluster/k8s/inventree/token-provisioner:push", "ghcr.io/agentydragon/token-provisioner"),
    ("//props/backend:push", "ghcr.io/agentydragon/props-backend"),
    ("//airlock:push", "ghcr.io/agentydragon/airlock"),
    ("//airlock/auth_proxy:push", "ghcr.io/agentydragon/auth-proxy"),
    ("//mcp_infra/exec:direct_push", "ghcr.io/agentydragon/exec-backend"),
    ("//openclaw/exec:push", "ghcr.io/agentydragon/openclaw-exec"),
    ("//homeassistant/proxy:push", "ghcr.io/agentydragon/homeassistant-proxy"),
    ("//inventree_utils/rai_plugin:push", "ghcr.io/agentydragon/inventree"),
    ("//tana/token_broker:push", "ghcr.io/agentydragon/tana-token-broker"),
    ("//third_party/activitywatch:push", "ghcr.io/agentydragon/aw-server"),
]


def crane(*args: str) -> str:
    """Run crane via bazel and return stdout."""
    result = subprocess.run([*_BAZEL_RUN, "@crane", "--", *args], check=True, capture_output=True, text=True)
    return result.stdout.strip()


def setup_ghcr_auth(username: str, token: str) -> None:
    """Write ~/.docker/config.json for crane registry auth."""
    docker_dir = Path.home() / ".docker"
    docker_dir.mkdir(exist_ok=True)
    auth = base64.b64encode(f"{username}:{token}".encode()).decode()
    config = {"auths": {"ghcr.io": {"auth": auth}}}
    (docker_dir / "config.json").write_text(json.dumps(config))


def latest_pinned_tag(repo: str, branch: str) -> str | None:
    """Find the most recent pinned tag for the given branch, or None."""
    try:
        tags = crane("ls", repo).splitlines()
    except subprocess.CalledProcessError:
        return None
    branch_tags = sorted(t for t in tags if t.startswith(f"{branch}-"))
    return branch_tags[-1] if branch_tags else None


def push_and_tag(target: str, repo: str, pinned_tag: str, branch: str) -> None:
    print(f"Pushing {target}")
    subprocess.run([*_BAZEL_RUN, target], check=True)

    new_digest = crane("digest", f"{repo}:latest")

    current_tag = latest_pinned_tag(repo, branch)
    if current_tag:
        current_digest = crane("digest", f"{repo}:{current_tag}")
        if new_digest == current_digest:
            print(f"{repo}: digest unchanged ({new_digest[:19]}), skipping tag")
            return

    print(f"{repo}: tagging {pinned_tag} (digest: {new_digest[:19]})")
    crane("tag", f"{repo}:latest", pinned_tag)


def main() -> None:
    workspace = os.environ.get("BUILD_WORKSPACE_DIRECTORY")
    if workspace:
        os.chdir(workspace)

    subject = subprocess.check_output(["git", "log", "-1", "--format=%s"], text=True).strip()
    if "[skip ci]" in subject:
        print("Commit message contains [skip ci], skipping image push.")
        return

    setup_ghcr_auth(get_required_env("GHCR_USERNAME"), get_required_env("GHCR_TOKEN"))

    branch = subprocess.check_output(["git", "rev-parse", "--abbrev-ref", "HEAD"], text=True).strip()
    ts = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
    sha = subprocess.check_output(["git", "rev-parse", "--short=7", "HEAD"], text=True).strip()
    pinned_tag = f"{branch}-{ts}-{sha}"

    for target, repo in IMAGE_TARGETS:
        push_and_tag(target, repo, pinned_tag, branch)


if __name__ == "__main__":
    main()
