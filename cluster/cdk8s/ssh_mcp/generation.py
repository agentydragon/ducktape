"""Synthesize SSH MCP's backend and sshpiper Pipe into their Kustomize directories."""

from __future__ import annotations

from pathlib import Path

from cdk8s import App, Chart
from flux_kustomize.io.fluxcd.toolkit.kustomize import KustomizationSpecDeletionPolicy

from cluster.cdk8s.fleet_rules import add_fleet_rules
from cluster.cdk8s.flux import Kustomization, RenderedDirectory, flux_kustomization, flux_kustomization_depends_on_many
from cluster.cdk8s.manifest_roots import HAND_WRITTEN_ROOT
from cluster.cdk8s.ssh_mcp import backend, config, sshpiper
from cluster.scripts.nebula_mesh import Mesh

OUTPUT_DIR = f"{HAND_WRITTEN_ROOT}/ssh-mcp"
_SSHPIPER_OUTPUT_DIR = f"{HAND_WRITTEN_ROOT}/agents/public-coder-agent/sshpiper"
KEY_FILES = ("keys-atlas.sops.yaml", "keys-public-coder-devbox.sops.yaml", "keys.sops.yaml")


def chart(app: App, *, ssh_config: config.SshMcpConfig, mesh: Mesh) -> Chart:
    chart = backend.chart(app, config=ssh_config, mesh=mesh)
    add_fleet_rules(chart)
    return chart


def write_sshpiper_pipe(root: Path, ssh_config: config.SshMcpConfig) -> None:
    """Write the devbox Pipe into public-coder-agent's sshpiper directory, whose generated
    `kustomization.yaml` lists it."""
    sshpiper_dir = root / _SSHPIPER_OUTPUT_DIR
    sshpiper_dir.mkdir(parents=True, exist_ok=True)
    pipe_app = App(outdir=str(sshpiper_dir))
    pipe_chart = Chart(pipe_app, "pipe-devbox", disable_resource_name_hashes=True)
    sshpiper.construct(pipe_chart, config=ssh_config, downstream_key=ssh_config.agent_downstream_key)
    pipe_app.synth()


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
