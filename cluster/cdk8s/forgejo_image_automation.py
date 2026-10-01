"""Flux image automation for the CI images in our Forgejo registry: one ImageRepository
scanning each image and one ImagePolicy selecting its newest tag.

Every entry has the same shape -- same registry, scan interval, pull credential and tag
policy -- so the roster below is just the names. The `ImageUpdateAutomation` that writes
the selected tags back into each directory's `image-pins/` Component lives in
`cluster/generated/flux-image-automation-ghcr`, not here; nothing in this chart carries an
`$imagepolicy` marker, so Flux never rewrites this generated file.
"""

from __future__ import annotations

from cdk8s import ApiObjectMetadata, App, Chart
from constructs import Construct
from flux_imagerepository_crds.io.fluxcd.toolkit.image import ImageRepositorySpecSecretRef

from cluster.cdk8s.fleet_rules import add_fleet_rules
from cluster.cdk8s.flux import Kustomization, RenderedDirectory, flux_kustomization, flux_kustomization_depends_on_many
from cluster.cdk8s.image_automation import newest_ci_tag_policy
from cluster.cdk8s.manifest_roots import GENERATED_ROOT
from cluster.cdk8s.providers.flux.image_repository import ImageRepository

NAME = "flux-image-automation-forgejo"
# Flux's own namespace, where the image-reflector controller reads these.
NAMESPACE = "flux-system"
OUTPUT_DIR = f"{GENERATED_ROOT}/flux-image-automation-forgejo"
_REGISTRY = "git.allegedly.works/ducktape-ci"
_SCAN_INTERVAL = "5m"
# The ducktape-ci pull credential, reflected here from cluster/k8s/forgejo-images/;
# the registry is private, so an unauthenticated scan finds nothing.
_PULL_SECRET = "forgejo-images-creds"

# Every CI image Flux watches. The ImagePolicy takes the entry's name, and that name is
# what an image-pins/ Component's marker references
# (`{"$imagepolicy": "flux-system:<name>:tag"}`), so renaming one is a coordinated change
# across every directory that pins the image -- not a rename here.
IMAGES = (
    # keep-sorted start
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
    "agentplane-sandbox",
    "agentplane-sandbox-build",
    "aiquota-api",
    "airlock",
    "attic-jwt-rotation",
    "authentik-jwt-rotation",
    "aw-server",
    "claude-session-sync",
    "cli-proxy-api",
    "cpap-gateway",
    "cpap-sync",
    "forgejo-token-rotation",
    "github-api-proxy",
    "github-graphql-rate-exporter",
    "google-mcp",
    "grocy-mcp-oidc-server",
    "grocy-user-perms-provisioner",
    "haku-console",
    "haku-console-static",
    "haku-kube-api-proxy",
    "haku-openclaw-spike",
    # The trailing `-image` is in the repository path too, unlike every other entry.
    "haku-sandbox-image",
    "homeassistant-component-installer",
    "homeassistant-onboarding",
    "homeassistant-token-provisioner",
    "iron-proxy",
    "loki-read-proxy",
    "matrix-user-provisioner",
    "mcp-oauth-facade",
    "osm-mcp",
    "plaid-mcp-server",
    "plaid-mcp-sync",
    "plaid-spend",
    "props-backend",
    "props-llm-proxy",
    "props-registry-proxy",
    "public-coder-agent",
    "public-coder-devbox",
    "rtl-tcp",
    "ssh-mcp",
    "stalwart",
    "study-casino",
    "tana-firebase-resigner",
    "tana-litellm-proxy",
    "tana-mcp",
    "vm-image-restart",
    # keep-sorted end
)

# Repository path for the entries whose image is not named after their policy.
_REPOSITORIES = {"grocy-mcp-oidc-server": "grocy-mcp", "tana-mcp": "tana-desktop"}


class ForgejoImageAutomation(Construct):
    """The ImageRepository/ImagePolicy pair for every image in `IMAGES`."""

    def __init__(self, scope: Construct, id: str) -> None:
        super().__init__(scope, id)
        for name in IMAGES:
            newest_ci_tag_policy(
                self,
                ImageRepository(
                    self,
                    f"{name}-repository",
                    metadata=ApiObjectMetadata(name=name, namespace=NAMESPACE),
                    image=f"{_REGISTRY}/{_REPOSITORIES.get(name, name)}",
                    interval=_SCAN_INTERVAL,
                    secret_ref=ImageRepositorySpecSecretRef(name=_PULL_SECRET),
                ),
            )


def chart(app: App) -> Chart:
    """The ImageRepository/ImagePolicy pair per CI image, as one chart. The directory had
    one hand-written file per pair; a single generated file keeps each pair adjacent
    (cluster/AGENTS.md § Colocating image repository and policy)."""
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    ForgejoImageAutomation(chart, "images")
    add_fleet_rules(chart)
    return chart


def flux_image_automation_forgejo(
    chart: Chart, directory: RenderedDirectory, flux_image_automation_ghcr: Kustomization
) -> Kustomization:
    return flux_kustomization(
        chart,
        NAME,
        directory,
        retry_interval=None,
        wait=None,
        depends_on=flux_kustomization_depends_on_many(flux_image_automation_ghcr),
        description=(
            "Image automation for images hosted in our Forgejo registry "
            "(authenticated scans via the reflected ducktape-ci credential)."
        ),
    )
