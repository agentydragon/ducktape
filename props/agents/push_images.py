"""Push all props agent images to the props registry via crane.

Reads registry URL and credentials from environment variables. Each image is
pushed with both a :latest tag and a :SHA tag (for pinning).

Usage:
    bazel run //props/agents:push_images
"""

import logging
import subprocess

from util.bazel.runfiles import get_required_path
from util.crane import Crane
from util.env import get_required_env
from util.oci import read_oci_layout_digest

logger = logging.getLogger(__name__)

# (rlocation of OCI layout dir, repository name, tag override for variants)
IMAGES: list[tuple[str, str, str | None]] = [
    ("_main/props/agents/critic/image", "critic", None),
    ("_main/props/agents/grader/image", "grader", None),
    ("_main/props/agents/critic_dev/improve/image", "critic_dev_improve", None),
    ("_main/props/agents/critic_dev/optimize/image", "critic_dev_optimize", None),
    ("_main/props/agents/critic/variants/contract_truthfulness", "critic", "contract_truthfulness"),
    ("_main/props/agents/critic/variants/dead_code", "critic", "dead_code"),
    ("_main/props/agents/critic/variants/flag_propagation", "critic", "flag_propagation"),
    ("_main/props/agents/critic/variants/high_recall", "critic", "high_recall"),
    ("_main/props/agents/critic/variants/verbose_docs", "critic", "verbose_docs"),
]


def _git_sha() -> str:
    return subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    registry = get_required_env("PROPS_REGISTRY_URL")
    username = get_required_env("PROPS_REGISTRY_USERNAME")
    password = get_required_env("PROPS_REGISTRY_PASSWORD")
    sha = _git_sha()

    crane = Crane(registry=registry, username=username, password=password)

    for rlocation, repo_name, variant_tag in IMAGES:
        image_dir = get_required_path(rlocation)
        local_digest = read_oci_layout_digest(image_dir)

        base_tag = variant_tag or "latest"
        sha_tag = f"{variant_tag}-{sha}" if variant_tag else sha

        # Content dedup: the images are Bazel-reproducible, so on commits that
        # don't change an agent image its digest is identical to what's already
        # published. Re-pushing re-PUTs the moving tag's manifest, which the
        # registry proxy turns into a grader_definition_changed notify ->
        # GraderSupervisor restarts every grader (cancelling in-flight grades).
        # Skip the no-op, mirroring the GHCR matrix dedup in push-images.yml.
        base_ref = f"{registry}/{repo_name}:{base_tag}"
        if crane.digest_or_none(base_ref) == local_digest:
            print(f"{base_ref}: digest unchanged ({local_digest[:19]}) — skipping")
            continue

        ref = f"{registry}/{repo_name}@{local_digest}"
        print(f"{base_ref}: pushing {local_digest[:19]}")
        crane.push(image_dir, ref)
        crane.tag(ref, base_tag)
        crane.tag(ref, sha_tag)
        print(f"  tagged {base_tag}, {sha_tag}")


if __name__ == "__main__":
    main()
