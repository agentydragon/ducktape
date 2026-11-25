"""Authenticated Flux scans for the ducktape-ci Forgejo registry.

This chart tracks devel tags. Platform Sessions declares its provider images in its own
chart and tracks main tags. The tenant Receiver filters package pushes for both rosters.
The ImageUpdateAutomation in flux-image-automation-ghcr writes the selected tags to
each workload's image-pins/ Component.
"""

from __future__ import annotations

from cdk8s import ApiObjectMetadata, App, Chart
from flux_imagerepository_crds.io.fluxcd.toolkit.image import (
    ImageRepository,
    ImageRepositorySpec,
    ImageRepositorySpecSecretRef,
)

from cluster.cdk8s.flux import Kustomization, RenderedDirectory, flux_kustomization, flux_kustomization_depends_on_many
from cluster.cdk8s.forgejo_registry import pull_credentials
from cluster.cdk8s.image_automation import newest_ci_tag_policy
from cluster.cdk8s.manifest_roots import GENERATED_ROOT

NAME = "flux-image-automation-forgejo"
# Flux's own namespace, where the image-reflector controller reads these.
NAMESPACE = "flux-system"
OUTPUT_DIR = f"{GENERATED_ROOT}/flux-image-automation-forgejo"
REGISTRY = "git.allegedly.works/ducktape-ci"

# The ImagePolicy takes the entry's name, and that name is
# what an image-pins/ Component's marker references
# (`{"$imagepolicy": "flux-system:<name>:tag"}`), so renaming one is a coordinated change
# across every directory that pins the image -- not a rename here.
IMAGES = (
    # keep-sorted start
    "agentplane-action-service",
    "agentplane-app",
    "agentplane-central-sampler",
    "agentplane-egress-proxy",
    "agentplane-egress-sidecar",
    "agentplane-environment-firecracker",
    "agentplane-environment-vm",
    "agentplane-environments",
    "agentplane-firecracker-launcher",
    "agentplane-frontend",
    "agentplane-history-service",
    "agentplane-llm-ingress",
    "agentplane-mcp-edge",
    "agentplane-notification-service",
    "agentplane-policy-controller",
    "agentplane-runner-disk",
    "agentplane-sandbox",
    "agentplane-sandbox-build",
    "agentplane-sessions",
    "ai-session-archive-importer",
    "ai-session-archive-web",
    "aiquota-api",
    "airlock",
    "att-gateway-exporter",
    "attic-jwt-rotation",
    "authentik-jwt-rotation",
    "aw-server",
    "claude-session-sync",
    "cli-proxy-api",
    "cpap-gateway",
    "cpap-sync",
    "crossplane-provider-buildbuddy",
    "environment-tools",
    "forgejo-token-rotation",
    "github-graphql-rate-exporter",
    "google-mcp",
    "grocy-mcp-oidc-server",
    "grocy-user-perms-provisioner",
    "haku-console",
    "haku-console-static",
    "homeassistant-component-installer",
    "homeassistant-onboarding",
    "homeassistant-token-provisioner",
    "loki-read-proxy",
    "matrix-user-provisioner",
    "mcp-oauth-facade",
    "plaid-mcp-server",
    "plaid-mcp-sync",
    "plaid-spend",
    "platform-sessions-gateway",
    "props-backend",
    "props-llm-proxy",
    "props-registry-proxy",
    "public-coder-agent",
    "roaming-node-controller",
    "rtl-tcp",
    "ssh-mcp",
    "stalwart",
    "study-casino",
    "tana-firebase-resigner",
    "tana-litellm-proxy",
    "tana-mcp",
    "vm-image-restart",
    "website",
    # keep-sorted end
)

# Repository path for the entries whose image is not named after their policy.
_REPOSITORIES = {"grocy-mcp-oidc-server": "grocy-mcp", "tana-mcp": "tana-desktop"}


def package_name(image: str) -> str:
    """The Forgejo package name used by both the registry scan and the push webhook."""
    return _REPOSITORIES.get(image, image)


def track_ci_image(chart: Chart, name: str, *, prefix: str) -> None:
    """Scan the private package in flux-system and select its newest CI tag for the given branch."""
    repository = ImageRepository(
        chart,
        f"{name}-repository",
        metadata=ApiObjectMetadata(name=name, namespace=NAMESPACE),
        spec=ImageRepositorySpec(
            image=f"{REGISTRY}/{package_name(name)}",
            interval="5m",
            secret_ref=ImageRepositorySpecSecretRef(name=pull_credentials.SECRET_NAME),
        ),
    )
    newest_ci_tag_policy(chart, repository, prefix=prefix)


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    for name in IMAGES:
        track_ci_image(chart, name, prefix="devel")
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
