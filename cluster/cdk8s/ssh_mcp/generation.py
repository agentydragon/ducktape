"""SSH MCP's Kustomization and hand-written encrypted key siblings."""

from __future__ import annotations

from cdk8s import Chart
from flux_kustomize.io.fluxcd.toolkit.kustomize import KustomizationSpecDeletionPolicy

from cluster.cdk8s.flux import Kustomization, RenderedDirectory, flux_kustomization, flux_kustomization_depends_on_many
from cluster.cdk8s.manifest_roots import HAND_WRITTEN_ROOT
from cluster.cdk8s.ssh_mcp import config

OUTPUT_DIR = f"{HAND_WRITTEN_ROOT}/ssh-mcp"
KEY_FILES = ("keys-agentplane.sops.yaml", "keys-atlas.sops.yaml", "keys.sops.yaml")


def ssh_mcp(flux_chart: Chart, directory: RenderedDirectory, external_secrets_operator: Kustomization) -> Kustomization:
    return flux_kustomization(
        flux_chart,
        config.NAME,
        directory,
        description="SSH MCP backend for Agentplane staging.",
        timeout="5m",
        deletion_policy=KustomizationSpecDeletionPolicy.ORPHAN,
        depends_on=flux_kustomization_depends_on_many(external_secrets_operator),
    )
