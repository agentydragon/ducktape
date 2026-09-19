"""Dispatch manifest generation to each component's local cdk8s helpers."""

from pathlib import Path

from cluster.cdk8s import (
    aiquota,
    cnpg_flux_kustomizations,
    descheduler,
    descheduler_flux_kustomizations,
    dns_automation,
    dns_automation_flux_kustomizations,
    egress_fences,
    etcd,
    external_creds,
    forgejo_image_automation,
    forgejo_images_flux_kustomizations,
    gateway_flux_kustomizations,
    ha_mcp,
    haku_openclaw_spike_config,
    ntfy,
    public_coder_agent_config,
    public_coder_devbox,
    stateful_infra,
)
from cluster.cdk8s.activitywatch import flux_kustomizations as activitywatch_flux_kustomizations
from cluster.cdk8s.agentplane import generation as agentplane_generation, staging, testing
from cluster.cdk8s.agentplane_crds import flux_kustomizations as agentplane_crds_flux_kustomizations
from cluster.cdk8s.agentplane_egress_credentials import (
    flux_kustomizations as agentplane_egress_credentials_flux_kustomizations,
)
from cluster.cdk8s.agentplane_index import flux_kustomizations as agentplane_index_flux_kustomizations
from cluster.cdk8s.agents import flux_kustomizations as agents_flux_kustomizations
from cluster.cdk8s.artifact_generators import generate_artifact_generators
from cluster.cdk8s.atuin import flux_kustomizations as atuin_flux_kustomizations
from cluster.cdk8s.authentik import flux_kustomizations as authentik_flux_kustomizations
from cluster.cdk8s.cert_manager import flux_kustomizations as cert_manager_flux_kustomizations
from cluster.cdk8s.cli_proxy_api import flux_kustomizations as cli_proxy_api_flux_kustomizations
from cluster.cdk8s.clickhouse import flux_kustomizations as clickhouse_flux_kustomizations, schema as clickhouse_schema
from cluster.cdk8s.coredns_custom import flux_kustomizations as coredns_custom_flux_kustomizations
from cluster.cdk8s.cpap_sync import flux_kustomizations as cpap_sync_flux_kustomizations
from cluster.cdk8s.dcgm_exporter import flux_kustomizations as dcgm_exporter_flux_kustomizations
from cluster.cdk8s.evidence import flux_kustomizations as evidence_flux_kustomizations
from cluster.cdk8s.external_secrets import flux_kustomizations as external_secrets_flux_kustomizations
from cluster.cdk8s.flux_grafana_secrets import flux_kustomizations as flux_grafana_secrets_flux_kustomizations
from cluster.cdk8s.flux_image_automation_forgejo import (
    flux_kustomizations as flux_image_automation_forgejo_flux_kustomizations,
)
from cluster.cdk8s.flux_image_automation_ghcr import (
    flux_kustomizations as flux_image_automation_ghcr_flux_kustomizations,
)
from cluster.cdk8s.flux_monitoring import flux_kustomizations as flux_monitoring_flux_kustomizations
from cluster.cdk8s.flux_webhook import flux_kustomizations as flux_webhook_flux_kustomizations
from cluster.cdk8s.flux_webhook_token import flux_kustomizations as flux_webhook_token_flux_kustomizations
from cluster.cdk8s.forgejo import flux_kustomizations as forgejo_flux_kustomizations
from cluster.cdk8s.gaffer_private_source import flux_kustomizations as gaffer_private_source_flux_kustomizations
from cluster.cdk8s.gatus import flux_kustomizations as gatus_flux_kustomizations
from cluster.cdk8s.github_api_proxy import flux_kustomizations as github_api_proxy_flux_kustomizations
from cluster.cdk8s.github_branch_protection import flux_kustomizations as github_branch_protection_flux_kustomizations
from cluster.cdk8s.github_exporter import flux_kustomizations as github_exporter_flux_kustomizations
from cluster.cdk8s.github_secrets_sync import flux_kustomizations as github_secrets_sync_flux_kustomizations
from cluster.cdk8s.goldilocks import flux_kustomizations as goldilocks_flux_kustomizations
from cluster.cdk8s.grafana import flux_kustomizations as grafana_flux_kustomizations
from cluster.cdk8s.grocy import flux_kustomizations as grocy_flux_kustomizations
from cluster.cdk8s.haku import charts as haku_charts, flux_kustomizations as haku_flux_kustomizations
from cluster.cdk8s.haku_ci import flux_kustomizations as haku_ci_flux_kustomizations
from cluster.cdk8s.headlamp import flux_kustomizations as headlamp_flux_kustomizations
from cluster.cdk8s.home_assistant import flux_kustomizations as home_assistant_flux_kustomizations
from cluster.cdk8s.hubble_ui import flux_kustomizations as hubble_ui_flux_kustomizations
from cluster.cdk8s.infra_drift import flux_kustomizations as infra_drift_flux_kustomizations
from cluster.cdk8s.keda import flux_kustomizations as keda_flux_kustomizations
from cluster.cdk8s.kube_api_proxy import flux_kustomizations as kube_api_proxy_flux_kustomizations
from cluster.cdk8s.kube_system import flux_kustomizations as kube_system_flux_kustomizations
from cluster.cdk8s.kubevirt import flux_kustomizations as kubevirt_flux_kustomizations
from cluster.cdk8s.kvm_device_plugin import flux_kustomizations as kvm_device_plugin_flux_kustomizations
from cluster.cdk8s.kyverno import flux_kustomizations as kyverno_flux_kustomizations
from cluster.cdk8s.langfuse import flux_kustomizations as langfuse_flux_kustomizations
from cluster.cdk8s.litellm import (
    credentials as litellm_credentials,
    flux_kustomizations as litellm_flux_kustomizations,
    keys as litellm_keys,
    proxy as litellm_proxy,
)
from cluster.cdk8s.local_path_provisioner import flux_kustomizations as local_path_provisioner_flux_kustomizations
from cluster.cdk8s.matrix import flux_kustomizations as matrix_flux_kustomizations
from cluster.cdk8s.metrics_server import flux_kustomizations as metrics_server_flux_kustomizations
from cluster.cdk8s.monitoring import flux_kustomizations as monitoring_flux_kustomizations
from cluster.cdk8s.nix_cache import flux_kustomizations as nix_cache_flux_kustomizations
from cluster.cdk8s.node_feature_discovery import flux_kustomizations as node_feature_discovery_flux_kustomizations
from cluster.cdk8s.nvidia_device_plugin import flux_kustomizations as nvidia_device_plugin_flux_kustomizations
from cluster.cdk8s.nvidia_runtimeclass import flux_kustomizations as nvidia_runtimeclass_flux_kustomizations
from cluster.cdk8s.oci_cache import flux_kustomizations as oci_cache_flux_kustomizations
from cluster.cdk8s.ollama import flux_kustomizations as ollama_flux_kustomizations
from cluster.cdk8s.openebs_lvm import flux_kustomizations as openebs_lvm_flux_kustomizations
from cluster.cdk8s.parked import flux_kustomizations as parked_flux_kustomizations
from cluster.cdk8s.proxmox_proxy import flux_kustomizations as proxmox_proxy_flux_kustomizations
from cluster.cdk8s.reflector import flux_kustomizations as reflector_flux_kustomizations
from cluster.cdk8s.reloader import flux_kustomizations as reloader_flux_kustomizations
from cluster.cdk8s.seaweedfs import flux_kustomizations as seaweedfs_flux_kustomizations
from cluster.cdk8s.seaweedfs_csi import flux_kustomizations as seaweedfs_csi_flux_kustomizations
from cluster.cdk8s.snapshot_controller import flux_kustomizations as snapshot_controller_flux_kustomizations
from cluster.cdk8s.ssh_mcp import generation as ssh_mcp_generation
from cluster.cdk8s.sshpiper_crds import flux_kustomizations as sshpiper_crds_flux_kustomizations
from cluster.cdk8s.study_casino import flux_kustomizations as study_casino_flux_kustomizations
from cluster.cdk8s.talos_cloud_controller_manager import (
    flux_kustomizations as talos_cloud_controller_manager_flux_kustomizations,
)
from cluster.cdk8s.tofu_controller import flux_kustomizations as tofu_controller_flux_kustomizations
from cluster.cdk8s.tofu_state import flux_kustomizations as tofu_state_flux_kustomizations
from cluster.cdk8s.user_agentydragon import flux_kustomizations as user_agentydragon_flux_kustomizations
from cluster.cdk8s.valkey import flux_kustomizations as valkey_flux_kustomizations
from cluster.cdk8s.vector_talos_logs import flux_kustomizations as vector_talos_logs_flux_kustomizations
from cluster.cdk8s.vm_images_publisher import flux_kustomizations as vm_images_publisher_flux_kustomizations
from cluster.cdk8s.volsync import flux_kustomizations as volsync_flux_kustomizations
from cluster.cdk8s.vpa import flux_kustomizations as vpa_flux_kustomizations
from cluster.cdk8s.website import flux_kustomizations as website_flux_kustomizations
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
    agents_flux_kustomizations.write_manifests(root)
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
    seaweedfs_flux_kustomizations.write_manifests(root)
    egress_fences.write_manifests(root)
    dns_automation.write_manifests(root, mesh)
    litellm_keys.write_manifests(root)
    etcd.write_manifests(root, mesh)
    forgejo_image_automation.write_manifests(root)
    monitoring_flux_kustomizations.write_manifests(root)
    forgejo_flux_kustomizations.write_manifests(root)
    ntfy.write_manifests(root)
    litellm_credentials.write_agentplane_testing_manifests(root)
    website_flux_kustomizations.write_manifests(root)
    vpa_flux_kustomizations.write_manifests(root)
    volsync_flux_kustomizations.write_manifests(root)
    vm_images_publisher_flux_kustomizations.write_manifests(root)
    vector_talos_logs_flux_kustomizations.write_manifests(root)
    valkey_flux_kustomizations.write_manifests(root)
    user_agentydragon_flux_kustomizations.write_manifests(root)
    tofu_state_flux_kustomizations.write_manifests(root)
    tofu_controller_flux_kustomizations.write_manifests(root)
    talos_cloud_controller_manager_flux_kustomizations.write_manifests(root)
    study_casino_flux_kustomizations.write_manifests(root)
    sshpiper_crds_flux_kustomizations.write_manifests(root)
    snapshot_controller_flux_kustomizations.write_manifests(root)
    seaweedfs_csi_flux_kustomizations.write_manifests(root)
    reloader_flux_kustomizations.write_manifests(root)
    reflector_flux_kustomizations.write_manifests(root)
    proxmox_proxy_flux_kustomizations.write_manifests(root)
    parked_flux_kustomizations.write_manifests(root)
    openebs_lvm_flux_kustomizations.write_manifests(root)
    ollama_flux_kustomizations.write_manifests(root)
    oci_cache_flux_kustomizations.write_manifests(root)
    nvidia_runtimeclass_flux_kustomizations.write_manifests(root)
    nvidia_device_plugin_flux_kustomizations.write_manifests(root)
    node_feature_discovery_flux_kustomizations.write_manifests(root)
    nix_cache_flux_kustomizations.write_manifests(root)
    metrics_server_flux_kustomizations.write_manifests(root)
    matrix_flux_kustomizations.write_manifests(root)
    local_path_provisioner_flux_kustomizations.write_manifests(root)
    litellm_flux_kustomizations.write_manifests(root)
    langfuse_flux_kustomizations.write_manifests(root)
    kyverno_flux_kustomizations.write_manifests(root)
    kvm_device_plugin_flux_kustomizations.write_manifests(root)
    kubevirt_flux_kustomizations.write_manifests(root)
    kube_system_flux_kustomizations.write_manifests(root)
    kube_api_proxy_flux_kustomizations.write_manifests(root)
    keda_flux_kustomizations.write_manifests(root)
    infra_drift_flux_kustomizations.write_manifests(root)
    hubble_ui_flux_kustomizations.write_manifests(root)
    home_assistant_flux_kustomizations.write_manifests(root)
    headlamp_flux_kustomizations.write_manifests(root)
    haku_ci_flux_kustomizations.write_manifests(root)
    haku_flux_kustomizations.write_manifests(root)
    grocy_flux_kustomizations.write_manifests(root)
    grafana_flux_kustomizations.write_manifests(root)
    goldilocks_flux_kustomizations.write_manifests(root)
    github_secrets_sync_flux_kustomizations.write_manifests(root)
    github_exporter_flux_kustomizations.write_manifests(root)
    github_branch_protection_flux_kustomizations.write_manifests(root)
    github_api_proxy_flux_kustomizations.write_manifests(root)
    gatus_flux_kustomizations.write_manifests(root)
    gateway_flux_kustomizations.write_manifests(root)
    gaffer_private_source_flux_kustomizations.write_manifests(root)
    forgejo_images_flux_kustomizations.write_manifests(root)
    flux_webhook_token_flux_kustomizations.write_manifests(root)
    flux_webhook_flux_kustomizations.write_manifests(root)
    flux_monitoring_flux_kustomizations.write_manifests(root)
    flux_image_automation_ghcr_flux_kustomizations.write_manifests(root)
    flux_image_automation_forgejo_flux_kustomizations.write_manifests(root)
    flux_grafana_secrets_flux_kustomizations.write_manifests(root)
    external_secrets_flux_kustomizations.write_manifests(root)
    evidence_flux_kustomizations.write_manifests(root)
    dns_automation_flux_kustomizations.write_manifests(root)
    descheduler_flux_kustomizations.write_manifests(root)
    dcgm_exporter_flux_kustomizations.write_manifests(root)
    cpap_sync_flux_kustomizations.write_manifests(root)
    coredns_custom_flux_kustomizations.write_manifests(root)
    cnpg_flux_kustomizations.write_manifests(root)
    clickhouse_flux_kustomizations.write_manifests(root)
    cli_proxy_api_flux_kustomizations.write_manifests(root)
    cert_manager_flux_kustomizations.write_manifests(root)
    authentik_flux_kustomizations.write_manifests(root)
    atuin_flux_kustomizations.write_manifests(root)
    agentplane_index_flux_kustomizations.write_manifests(root)
    agentplane_egress_credentials_flux_kustomizations.write_manifests(root)
    agentplane_crds_flux_kustomizations.write_manifests(root)
    activitywatch_flux_kustomizations.write_manifests(root)
    generate_artifact_generators(root)


def main() -> None:
    generate_manifests(get_build_workspace_directory())


if __name__ == "__main__":
    main()
