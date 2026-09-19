"""Dispatch manifest generation to each component's local cdk8s helpers."""

from pathlib import Path

from cluster.cdk8s import (
    aiquota,
    descheduler,
    dns_automation,
    egress_fences,
    etcd,
    external_creds,
    forgejo_image_automation,
    ha_mcp,
    haku_openclaw_spike_config,
    ntfy,
    public_coder_agent_config,
    public_coder_devbox,
    stateful_infra,
)
from cluster.cdk8s.agentplane import generation as agentplane_generation, staging, testing
from cluster.cdk8s.artifact_generators import generate_artifact_generators
from cluster.cdk8s.clickhouse import schema as clickhouse_schema
from cluster.cdk8s.haku import charts as haku_charts
from cluster.cdk8s.headlamp import flux_kustomizations as headlamp_flux_kustomizations
from cluster.cdk8s.litellm import credentials as litellm_credentials, keys as litellm_keys, proxy as litellm_proxy
from cluster.cdk8s.ssh_mcp import generation as ssh_mcp_generation
from cluster.scripts import nebula_mesh
from util.bazel.runfiles import get_required_path
from util.bazel.workspace import get_build_workspace_directory


def generate_manifests(root: Path) -> None:
    """Write every converted directory's generated manifests under ``root``."""
    mesh = nebula_mesh.load(get_required_path("_main/nebula-mesh.json"))
    devbox_service = public_coder_devbox.write_manifests(root)
    ssh_mcp_generation.write_manifests(root, mesh, devbox_service)
    litellm_proxy.write_app(root)
    ha_mcp.write_manifests(root)
    external_creds.write_manifests(root)
    clickhouse_schema.write_manifests(root)
    aiquota.write_manifests(root)
    agentplane_generation.write_environment_manifests(root, staging.ENV, staging.chart)
    agentplane_generation.write_environment_manifests(root, testing.ENV, testing.chart)
    haku_charts.write_manifests(root)
    haku_openclaw_spike_config.write_manifests(root)
    public_coder_agent_config.write_manifests(root)
    descheduler.write_manifests(root)
    stateful_infra.write_seaweedfs_manifests(root)
    egress_fences.write_manifests(root)
    dns_automation.write_manifests(root, mesh)
    litellm_keys.write_manifests(root)
    etcd.write_manifests(root, mesh)
    forgejo_image_automation.write_manifests(root)
    ntfy.write_manifests(root)
    litellm_credentials.write_agentplane_testing_manifests(root)
    headlamp_flux_kustomizations.write_manifests(root)
    generate_artifact_generators(root)


def main() -> None:
    generate_manifests(get_build_workspace_directory())


if __name__ == "__main__":
    main()
