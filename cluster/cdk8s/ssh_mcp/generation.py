"""SSH MCP's backend chart and Kustomization, and the sshpiper Pipe chart for public-coder-agent's
sshpiper directory."""

from __future__ import annotations

from cdk8s import App, Chart
from flux_kustomize.io.fluxcd.toolkit.kustomize import KustomizationSpecDeletionPolicy

from cluster.cdk8s.fleet_rules import add_fleet_rules
from cluster.cdk8s.flux import Kustomization, RenderedDirectory, flux_kustomization, flux_kustomization_depends_on_many
from cluster.cdk8s.manifest_roots import HAND_WRITTEN_ROOT
from cluster.cdk8s.ssh_mcp import backend, config, sshpiper
from cluster.scripts.nebula_mesh import Mesh

OUTPUT_DIR = f"{HAND_WRITTEN_ROOT}/ssh-mcp"
KEY_FILES = ("keys-atlas.sops.yaml", "keys-public-coder-devbox.sops.yaml", "keys.sops.yaml")


def chart(app: App, *, ssh_config: config.SshMcpConfig, mesh: Mesh) -> Chart:
    chart = backend.chart(app, config=ssh_config, mesh=mesh)
    add_fleet_rules(chart)
    return chart


def sshpiper_pipe_chart(app: App, *, ssh_config: config.SshMcpConfig) -> Chart:
    """The devbox Pipe, which `public_coder_sshpiper.write_manifests` writes into
    public-coder-agent's sshpiper directory."""
    chart = Chart(app, "pipe-devbox", disable_resource_name_hashes=True)
    sshpiper.construct(chart, config=ssh_config, downstream_key=ssh_config.agent_downstream_key)
    return chart


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
