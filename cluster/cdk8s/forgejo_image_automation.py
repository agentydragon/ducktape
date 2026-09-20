"""Flux image automation for the CI images in our Forgejo registry: one ImageRepository
scanning each image and one ImagePolicy selecting its newest tag.

Every entry has the same shape -- same registry, scan interval, pull credential and tag
policy -- so the roster below is just the names. The `ImageUpdateAutomation` that writes
the selected tags back into each directory's `image-pins/` Component lives in
`cluster/k8s/flux-image-automation-ghcr`, not here; nothing in this chart carries an
`$imagepolicy` marker, so Flux never rewrites this generated file.
"""

from __future__ import annotations

from pathlib import Path

from cdk8s import App, Chart
from constructs import Construct
from flux_imagepolicy_crds.io.fluxcd.toolkit.image import (
    ImagePolicy,
    ImagePolicySpec,
    ImagePolicySpecFilterTags,
    ImagePolicySpecImageRepositoryRef,
    ImagePolicySpecPolicy,
    ImagePolicySpecPolicyAlphabetical,
    ImagePolicySpecPolicyAlphabeticalOrder,
)
from flux_imagerepository_crds.io.fluxcd.toolkit.image import (
    ImageRepository,
    ImageRepositorySpec,
    ImageRepositorySpecSecretRef,
)

from cluster.cdk8s.fleet_rules import add_fleet_rules
from cluster.cdk8s.flux import kustomize_kustomization
from cluster.cdk8s.generation import write_yaml
from cluster.cdk8s.metadata import metadata

NAME = "flux-image-automation-forgejo"
# Flux's own namespace, where the image-reflector controller reads these.
NAMESPACE = "flux-system"
OUTPUT_DIR = "cluster/k8s/flux-image-automation-forgejo"
_REGISTRY = "git.allegedly.works/ducktape-ci"
_SCAN_INTERVAL = "5m"
# The ducktape-ci pull credential, reflected here from cluster/k8s/forgejo-images/;
# the registry is private, so an unauthenticated scan finds nothing.
_PULL_SECRET = "forgejo-images-creds"
# CI pushes {branch}-YYYYMMDDHHMMSS-{sha7}, which makes alphabetical order chronological.
_TAG_PATTERN = r"^devel-\d{14}-[0-9a-f]{7}$"

# Every CI image Flux watches. The ImagePolicy takes the entry's name, and that name is
# what an image-pins/ Component's marker references
# (`{"$imagepolicy": "flux-system:<name>:tag"}`), so renaming one is a coordinated change
# across every directory that pins the image -- not a rename here.
IMAGES = (
    "agent-workspace",
    "agentplane-action-service",
    "agentplane-action-service-migrate",
    "agentplane-app",
    "agentplane-app-migrate",
    "agentplane-egress",
    "agentplane-egress-migrate",
    "agentplane-egress-sidecar",
    "agentplane-index",
    "agentplane-llm-ingress",
    "agentplane-oauth-fixture",
    "agentplane-runner",
    "aiquota-api",
    "airlock",
    "attic-jwt-rotation",
    "authentik-jwt-rotation",
    "aw-server",
    "cli-proxy-api",
    "codex-pod",
    "cpap-gateway",
    "cpap-sync",
    "forgejo-token-rotation",
    "github-api-proxy",
    "github-graphql-rate-exporter",
    "grocy-mcp-oidc-server",
    "grocy-user-perms-provisioner",
    "ha-mcp-token-provisioner",
    "haku-console",
    "haku-console-static",
    "haku-kube-api-proxy",
    "haku-managed-agent",
    "haku-openclaw-spike",
    # The trailing `-image` is in the repository path too, unlike every other entry.
    "haku-sandbox-image",
    "homeassistant-provisioner",
    "iron-proxy",
    "loki-read-proxy",
    "manifold-mcp-server",
    "matrix-user-provisioner",
    "mcp-oauth-facade",
    "osm-mcp",
    "plaid-mcp-server",
    "plaid-mcp-sync",
    "postscanmail-mcp-server",
    "props-backend",
    "props-llm-proxy",
    "props-registry-proxy",
    "public-coder-devbox",
    "rtl-tcp",
    "ssh-mcp",
    "stalwart",
    "study-casino",
    "tana-firebase-resigner",
    "tana-litellm-proxy",
    "tana-mcp",
)

# Repository path for the entries whose image is not named after their policy.
_REPOSITORIES = {"grocy-mcp-oidc-server": "grocy-mcp", "tana-mcp": "tana-desktop"}


class ForgejoImageAutomation(Construct):
    """The ImageRepository/ImagePolicy pair for every image in `IMAGES`."""

    def __init__(self, scope: Construct, id: str) -> None:
        super().__init__(scope, id)
        for name in IMAGES:
            ImageRepository(
                self,
                f"{name}-repository",
                metadata=metadata(name, NAMESPACE),
                spec=ImageRepositorySpec(
                    image=f"{_REGISTRY}/{_REPOSITORIES.get(name, name)}",
                    interval=_SCAN_INTERVAL,
                    secret_ref=ImageRepositorySpecSecretRef(name=_PULL_SECRET),
                ),
            )
            ImagePolicy(
                self,
                f"{name}-policy",
                metadata=metadata(name, NAMESPACE),
                spec=ImagePolicySpec(
                    image_repository_ref=ImagePolicySpecImageRepositoryRef(name=name),
                    filter_tags=ImagePolicySpecFilterTags(pattern=_TAG_PATTERN),
                    policy=ImagePolicySpecPolicy(
                        alphabetical=ImagePolicySpecPolicyAlphabetical(order=ImagePolicySpecPolicyAlphabeticalOrder.ASC)
                    ),
                ),
            )


def write_manifests(root: Path) -> None:
    """The ImageRepository/ImagePolicy pair per CI image, as one chart. The directory had
    one hand-written file per pair; a single generated file keeps each pair adjacent
    (cluster/AGENTS.md § Colocating image repository and policy)."""
    out_dir = root / OUTPUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    app = App(outdir=str(out_dir))
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    ForgejoImageAutomation(chart, "images")
    add_fleet_rules(chart, provided_secrets={}, providers=frozenset())
    app.synth()

    write_yaml(out_dir / "kustomization.yaml", kustomize_kustomization(resources=[f"{NAME}.k8s.yaml"]))
