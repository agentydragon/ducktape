"""Dispatch manifest generation to each component's local cdk8s helpers."""

from pathlib import Path

from cdk8s import App, Chart

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
from cluster.cdk8s.artifact_generators import artifact_generators as artifact_generators_factory
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
from cluster.cdk8s.flux import health_checks as flux_health_checks
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
    agentplane_staging_resource_chart = agentplane_generation.write_environment_manifests(
        root, staging.ENV, staging.chart
    )
    agentplane_testing_resource_chart = agentplane_generation.write_environment_manifests(
        root, testing.ENV, testing.chart
    )
    agentplane_staging_health_checks = agentplane_generation.environment_health_checks(
        agentplane_staging_resource_chart, staging.ENV.namespace
    )
    agentplane_testing_health_checks = agentplane_generation.environment_health_checks(
        agentplane_testing_resource_chart, testing.ENV.namespace
    )
    haku_console_resource_chart = haku_charts.write_console_manifests(root)
    haku_console_health_checks = flux_health_checks(haku_console_resource_chart, ("Cluster", "Job"))
    haku_openclaw_spike_config.write_manifests(root)
    public_coder_agent_config.write_manifests(root)
    descheduler.write_manifests(root)
    stateful_infra.write_seaweedfs_manifests(root)
    egress_fences.write_manifests(root)
    dns_automation.write_manifests(root, mesh)
    litellm_keys.write_manifests(root)
    forgejo_image_automation.write_manifests(root)
    litellm_credentials.write_agentplane_testing_manifests(root)

    flux_output = root / "cluster/k8s/flux"
    flux_output.mkdir(parents=True, exist_ok=True)
    flux_app = App(outdir=str(flux_output))
    flux_chart = Chart(flux_app, "kustomizations", disable_resource_name_hashes=True)
    agentplane_crds_kustomization = agentplane_crds_flux_kustomizations.agentplane_crds(flux_chart)
    agentplane_egress_credentials_namespace_kustomization = (
        agentplane_egress_credentials_flux_kustomizations.agentplane_egress_credentials_namespace(flux_chart)
    )
    agent_sandbox_controller_kustomization = agents_flux_kustomizations.agent_sandbox_controller(flux_chart)
    haku_egress_proxy_namespace_kustomization = agents_flux_kustomizations.haku_egress_proxy_namespace(flux_chart)
    haku_openclaw_spike_namespace_kustomization = agents_flux_kustomizations.haku_openclaw_spike_namespace(flux_chart)
    agents_mitmproxy_namespace_kustomization = agents_flux_kustomizations.agents_mitmproxy_namespace(flux_chart)
    public_coder_agent_namespace_kustomization = agents_flux_kustomizations.public_coder_agent_namespace(flux_chart)
    artifact_generators_factory(flux_chart, root)
    atuin_namespace_kustomization = atuin_flux_kustomizations.atuin_namespace(flux_chart)
    authentik_namespace_kustomization = authentik_flux_kustomizations.authentik_namespace(flux_chart)
    cert_manager_issuer_config_kustomization = cert_manager_flux_kustomizations.cert_manager_issuer_config(flux_chart)
    clickhouse_namespace_kustomization = clickhouse_flux_kustomizations.clickhouse_namespace(flux_chart)
    coredns_custom_flux_kustomizations.coredns_custom(flux_chart)
    evidence_flux_kustomizations.evidence_market_roster(flux_chart)
    external_secrets_crds_kustomization = external_secrets_flux_kustomizations.external_secrets_crds(flux_chart)
    flux_image_automation_ghcr_kustomization = (
        flux_image_automation_ghcr_flux_kustomizations.flux_image_automation_ghcr(flux_chart)
    )
    budget_namespace_kustomization = forgejo_flux_kustomizations.budget_namespace(flux_chart)
    forgejo_namespace_kustomization = forgejo_flux_kustomizations.forgejo_namespace(flux_chart)
    gatus_namespace_kustomization = gatus_flux_kustomizations.gatus_namespace(flux_chart)
    haku_mailbox_namespace_kustomization = haku_flux_kustomizations.haku_mailbox_namespace(flux_chart)
    haku_namespace_kustomization = haku_flux_kustomizations.haku_namespace(flux_chart)
    hubble_ui_flux_kustomizations.hubble_ui(flux_chart)
    kube_api_proxy_flux_kustomizations.kube_api_proxy(flux_chart)
    cdi_operator_kustomization = kubevirt_flux_kustomizations.cdi_operator(flux_chart)
    kubevirt_operator_kustomization = kubevirt_flux_kustomizations.kubevirt_operator(flux_chart)
    kvm_device_plugin_flux_kustomizations.kvm_device_plugin(flux_chart)
    kyverno_kustomization = kyverno_flux_kustomizations.kyverno(flux_chart)
    langfuse_namespace_kustomization = langfuse_flux_kustomizations.langfuse_namespace(flux_chart)
    litellm_namespace_kustomization = litellm_flux_kustomizations.litellm_namespace(flux_chart)
    local_path_provisioner_kustomization = local_path_provisioner_flux_kustomizations.local_path_provisioner(flux_chart)
    matrix_namespace_kustomization = matrix_flux_kustomizations.matrix_namespace(flux_chart)
    monitoring_crds_kustomization = monitoring_flux_kustomizations.monitoring_crds(flux_chart)
    grafana_helmrepository_kustomization = monitoring_flux_kustomizations.grafana_helmrepository(flux_chart)
    monitoring_namespace_kustomization = monitoring_flux_kustomizations.monitoring_namespace(flux_chart)
    node_feature_discovery_kustomization = node_feature_discovery_flux_kustomizations.node_feature_discovery(flux_chart)
    nvidia_runtimeclass_kustomization = nvidia_runtimeclass_flux_kustomizations.nvidia_runtimeclass(flux_chart)
    openebs_lvm_flux_kustomizations.openebs_lvm(flux_chart)
    browsertrix_namespace_kustomization = parked_flux_kustomizations.browsertrix_namespace(flux_chart)
    parked_flux_kustomizations.browsertrix_retained(flux_chart)
    parked_flux_kustomizations.buildbuddy_executor(flux_chart)
    parked_flux_kustomizations.egress_proxy_rugged(flux_chart)
    firecrawl_namespace_kustomization = parked_flux_kustomizations.firecrawl_namespace(flux_chart)
    gecko_namespace_kustomization = parked_flux_kustomizations.gecko_namespace(flux_chart)
    inventree_namespace_kustomization = parked_flux_kustomizations.inventree_namespace(flux_chart)
    openhands_namespace_kustomization = parked_flux_kustomizations.openhands_namespace(flux_chart)
    openhands_sandboxes_kustomization = parked_flux_kustomizations.openhands_sandboxes(flux_chart)
    paperless_namespace_kustomization = parked_flux_kustomizations.paperless_namespace(flux_chart)
    tandoor_namespace_kustomization = parked_flux_kustomizations.tandoor_namespace(flux_chart)
    reflector_kustomization = reflector_flux_kustomizations.reflector(flux_chart)
    seaweedfs_namespace_kustomization = seaweedfs_flux_kustomizations.seaweedfs_namespace(flux_chart)
    snapshot_controller_crds_kustomization = snapshot_controller_flux_kustomizations.snapshot_controller_crds(
        flux_chart
    )
    ssh_mcp_namespace_kustomization = ssh_mcp_generation.ssh_mcp_namespace(flux_chart)
    sshpiper_crds_kustomization = sshpiper_crds_flux_kustomizations.sshpiper_crds(flux_chart)
    study_casino_namespace_kustomization = study_casino_flux_kustomizations.study_casino_namespace(flux_chart)
    (talos_cloud_controller_manager_flux_kustomizations.talos_cloud_controller_manager(flux_chart))
    tofu_state_namespace_kustomization = tofu_state_flux_kustomizations.tofu_state_namespace(flux_chart)
    user_agentydragon_kustomization = user_agentydragon_flux_kustomizations.user_agentydragon(flux_chart)
    valkey_kustomization = valkey_flux_kustomizations.valkey(flux_chart)
    gaffer_private_source_flux_kustomizations.gaffer_private_source(
        flux_chart, flux_image_automation_ghcr_kustomization
    )
    haku_rbac_kustomization = haku_flux_kustomizations.haku_rbac(flux_chart, haku_namespace_kustomization)
    kubevirt_kustomization = kubevirt_flux_kustomizations.kubevirt(flux_chart, kubevirt_operator_kustomization)
    descheduler_flux_kustomizations.descheduler(flux_chart, kyverno_kustomization)
    keda_kustomization = keda_flux_kustomizations.keda(flux_chart, kyverno_kustomization)
    kyverno_policies_kustomization = kyverno_flux_kustomizations.kyverno_policies(flux_chart, kyverno_kustomization)
    metrics_server_kustomization = metrics_server_flux_kustomizations.metrics_server(flux_chart, kyverno_kustomization)
    reloader_kustomization = reloader_flux_kustomizations.reloader(flux_chart, kyverno_kustomization)
    cdi_kustomization = kubevirt_flux_kustomizations.cdi(
        flux_chart, cdi_operator_kustomization, local_path_provisioner_kustomization
    )
    clickhouse_operator_kustomization = clickhouse_flux_kustomizations.clickhouse_operator(
        flux_chart, clickhouse_namespace_kustomization, monitoring_crds_kustomization
    )
    flux_monitoring_flux_kustomizations.flux_monitoring(flux_chart, monitoring_crds_kustomization)
    monitoring_flux_kustomizations.cilium_monitoring(flux_chart, monitoring_crds_kustomization)
    etcd.etcd_monitoring(flux_chart, root, mesh, monitoring_crds_kustomization)
    monitoring_flux_kustomizations.monitoring_rules(flux_chart, monitoring_crds_kustomization)
    grafana_operator_kustomization = monitoring_flux_kustomizations.grafana_operator(
        flux_chart, monitoring_namespace_kustomization
    )
    nvidia_device_plugin_kustomization = nvidia_device_plugin_flux_kustomizations.nvidia_device_plugin(
        flux_chart, nvidia_runtimeclass_kustomization, node_feature_discovery_kustomization
    )
    agents_flux_kustomizations.coinbase_read(flux_chart, reflector_kustomization, haku_namespace_kustomization)
    cert_manager_kustomization = cert_manager_flux_kustomizations.cert_manager(
        flux_chart, cert_manager_issuer_config_kustomization, reflector_kustomization, monitoring_crds_kustomization
    )
    seaweedfs_operator_kustomization = seaweedfs_flux_kustomizations.seaweedfs_operator(
        flux_chart, seaweedfs_namespace_kustomization
    )
    snapshot_controller_kustomization = snapshot_controller_flux_kustomizations.snapshot_controller(
        flux_chart, snapshot_controller_crds_kustomization
    )
    agents_flux_kustomizations.public_coder_agent_sshpiper(
        flux_chart, public_coder_agent_namespace_kustomization, sshpiper_crds_kustomization
    )
    forgejo_cache_kustomization = forgejo_flux_kustomizations.forgejo_cache(
        flux_chart, forgejo_namespace_kustomization, valkey_kustomization, local_path_provisioner_kustomization
    )
    langfuse_cache_kustomization = langfuse_flux_kustomizations.langfuse_cache(
        flux_chart, langfuse_namespace_kustomization, valkey_kustomization, local_path_provisioner_kustomization
    )
    paperless_cache_kustomization = parked_flux_kustomizations.paperless_cache(
        flux_chart, paperless_namespace_kustomization, valkey_kustomization, local_path_provisioner_kustomization
    )
    haku_forgejo_tea_kustomization = haku_flux_kustomizations.haku_forgejo_tea(flux_chart, haku_rbac_kustomization)
    claude_rbac_kustomization = agents_flux_kustomizations.claude_rbac(flux_chart, kyverno_policies_kustomization)
    vpa_kustomization = vpa_flux_kustomizations.vpa(flux_chart, kyverno_kustomization, metrics_server_kustomization)
    clickhouse_kustomization = clickhouse_flux_kustomizations.clickhouse(flux_chart, clickhouse_operator_kustomization)
    dcgm_exporter_flux_kustomizations.dcgm_exporter(
        flux_chart, nvidia_device_plugin_kustomization, monitoring_crds_kustomization
    )
    cert_manager_trust_kustomization = cert_manager_flux_kustomizations.cert_manager_trust(
        flux_chart, cert_manager_kustomization, kyverno_kustomization
    )
    cnpg_kustomization = cnpg_flux_kustomizations.cnpg(flux_chart, cert_manager_kustomization)
    external_secrets_operator_kustomization = external_secrets_flux_kustomizations.external_secrets_operator(
        flux_chart, external_secrets_crds_kustomization, cert_manager_kustomization
    )
    gateway_kustomization = gateway_flux_kustomizations.gateway(
        flux_chart, cert_manager_kustomization, kyverno_kustomization, cert_manager_issuer_config_kustomization
    )
    tofu_controller_kustomization = tofu_controller_flux_kustomizations.tofu_controller(
        flux_chart, cert_manager_kustomization, kyverno_kustomization
    )
    volsync_kustomization = volsync_flux_kustomizations.volsync(flux_chart, snapshot_controller_kustomization)
    agents_flux_kustomizations.agent_shared_rbac(flux_chart, claude_rbac_kustomization, kyverno_policies_kustomization)
    agent_shared_secrets_kustomization = agents_flux_kustomizations.agent_shared_secrets(
        flux_chart, claude_rbac_kustomization
    )
    external_creds_kustomization = external_creds.external_creds(flux_chart, root, claude_rbac_kustomization)
    goldilocks_kustomization = goldilocks_flux_kustomizations.goldilocks(flux_chart, vpa_kustomization)
    clickhouse_schema_kustomization = clickhouse_schema.clickhouse_schema(flux_chart, root, clickhouse_kustomization)
    cert_manager_environment_kustomization = cert_manager_flux_kustomizations.cert_manager_environment(
        flux_chart,
        cert_manager_kustomization,
        cert_manager_trust_kustomization,
        cert_manager_issuer_config_kustomization,
    )
    atuin_db_kustomization = atuin_flux_kustomizations.atuin_db(
        flux_chart, atuin_namespace_kustomization, cnpg_kustomization, local_path_provisioner_kustomization
    )
    authentik_db_kustomization = authentik_flux_kustomizations.authentik_db(
        flux_chart, cnpg_kustomization, authentik_namespace_kustomization, local_path_provisioner_kustomization
    )
    forgejo_db_kustomization = forgejo_flux_kustomizations.forgejo_db(
        flux_chart, forgejo_namespace_kustomization, cnpg_kustomization
    )
    gatus_db_kustomization = gatus_flux_kustomizations.gatus_db(
        flux_chart, gatus_namespace_kustomization, cnpg_kustomization
    )
    haku_mailbox_db_kustomization = haku_flux_kustomizations.haku_mailbox_db(
        flux_chart, haku_mailbox_namespace_kustomization, cnpg_kustomization, local_path_provisioner_kustomization
    )
    langfuse_db_kustomization = langfuse_flux_kustomizations.langfuse_db(
        flux_chart, langfuse_namespace_kustomization, cnpg_kustomization, local_path_provisioner_kustomization
    )
    litellm_db_kustomization = litellm_flux_kustomizations.litellm_db(
        flux_chart, litellm_namespace_kustomization, cnpg_kustomization, local_path_provisioner_kustomization
    )
    matrix_db_kustomization = matrix_flux_kustomizations.matrix_db(
        flux_chart, matrix_namespace_kustomization, cnpg_kustomization, local_path_provisioner_kustomization
    )
    grafana_db_kustomization = monitoring_flux_kustomizations.grafana_db(
        flux_chart, monitoring_namespace_kustomization, cnpg_kustomization
    )
    firecrawl_db_kustomization = parked_flux_kustomizations.firecrawl_db(
        flux_chart, firecrawl_namespace_kustomization, cnpg_kustomization, local_path_provisioner_kustomization
    )
    inventree_db_kustomization = parked_flux_kustomizations.inventree_db(
        flux_chart, inventree_namespace_kustomization, cnpg_kustomization, local_path_provisioner_kustomization
    )
    paperless_db_kustomization = parked_flux_kustomizations.paperless_db(
        flux_chart, paperless_namespace_kustomization, cnpg_kustomization, local_path_provisioner_kustomization
    )
    tandoor_db_kustomization = parked_flux_kustomizations.tandoor_db(
        flux_chart, tandoor_namespace_kustomization, cnpg_kustomization, local_path_provisioner_kustomization
    )
    seaweedfs_filer_db_kustomization = seaweedfs_flux_kustomizations.seaweedfs_filer_db(
        flux_chart, seaweedfs_namespace_kustomization, cnpg_kustomization
    )
    study_casino_db_kustomization = study_casino_flux_kustomizations.study_casino_db(
        flux_chart,
        cnpg_kustomization,
        study_casino_namespace_kustomization,
        local_path_provisioner_kustomization,
        reflector_kustomization,
    )
    tofu_state_db_kustomization = tofu_state_flux_kustomizations.tofu_state_db(
        flux_chart, tofu_state_namespace_kustomization, cnpg_kustomization, local_path_provisioner_kustomization
    )
    external_secrets_config_kustomization = external_secrets_flux_kustomizations.external_secrets_config(
        flux_chart, external_secrets_operator_kustomization
    )
    seaweedfs_secrets_kustomization = seaweedfs_flux_kustomizations.seaweedfs_secrets(
        flux_chart, seaweedfs_namespace_kustomization, external_secrets_operator_kustomization
    )
    proxmox_proxy_flux_kustomizations.proxmox_proxy(flux_chart, gateway_kustomization)
    website_flux_kustomizations.website(flux_chart, gateway_kustomization)
    kube_system_flux_kustomizations.kube_system(flux_chart, goldilocks_kustomization)
    agents_flux_kustomizations.agents_mitmproxy(
        flux_chart,
        agents_mitmproxy_namespace_kustomization,
        cert_manager_environment_kustomization,
        cert_manager_trust_kustomization,
        reflector_kustomization,
    )
    github_api_proxy_identity_kustomization = github_api_proxy_flux_kustomizations.github_api_proxy_identity(
        flux_chart, cert_manager_environment_kustomization, cert_manager_issuer_config_kustomization
    )
    parked_flux_kustomizations.authelia(flux_chart, gateway_kustomization, cert_manager_environment_kustomization)
    parked_flux_kustomizations.docker_ci(flux_chart, cert_manager_environment_kustomization, claude_rbac_kustomization)
    atuin_kustomization = atuin_flux_kustomizations.atuin(
        flux_chart,
        cert_manager_issuer_config_kustomization,
        atuin_namespace_kustomization,
        atuin_db_kustomization,
        gateway_kustomization,
        cert_manager_environment_kustomization,
    )
    authentik_kustomization = authentik_flux_kustomizations.authentik(
        flux_chart,
        authentik_namespace_kustomization,
        authentik_db_kustomization,
        cert_manager_kustomization,
        gateway_kustomization,
        monitoring_crds_kustomization,
    )
    parked_flux_kustomizations.firecrawl(
        flux_chart, firecrawl_namespace_kustomization, firecrawl_db_kustomization, gateway_kustomization
    )
    parked_flux_kustomizations.paperless(
        flux_chart,
        paperless_namespace_kustomization,
        paperless_cache_kustomization,
        paperless_db_kustomization,
        gateway_kustomization,
    )
    dns_automation_flux_kustomizations.dns_automation(
        flux_chart, tofu_controller_kustomization, tofu_state_db_kustomization
    )
    forgejo_flux_kustomizations.forgejo_agentydragon(
        flux_chart, tofu_controller_kustomization, tofu_state_db_kustomization
    )
    infra_drift_flux_kustomizations.infra_drift(flux_chart, tofu_controller_kustomization, tofu_state_db_kustomization)
    (
        agentplane_egress_credentials_flux_kustomizations.agentplane_egress_credentials(
            flux_chart,
            agentplane_egress_credentials_namespace_kustomization,
            external_creds_kustomization,
            external_secrets_config_kustomization,
        )
    )
    agents_flux_kustomizations.alloy_otlp_bearer(
        flux_chart, external_secrets_config_kustomization, claude_rbac_kustomization, haku_rbac_kustomization
    )
    github_secrets_sync_secrets_kustomization = github_secrets_sync_flux_kustomizations.github_secrets_sync_secrets(
        flux_chart, external_creds_kustomization, external_secrets_config_kustomization
    )
    haku_console_namespace_kustomization = haku_flux_kustomizations.haku_console_namespace(
        flux_chart, external_secrets_config_kustomization
    )
    litellm_secrets_kustomization = litellm_flux_kustomizations.litellm_secrets(
        flux_chart, external_creds_kustomization, litellm_namespace_kustomization, external_secrets_config_kustomization
    )
    ntfy_kustomization = ntfy.ntfy(
        flux_chart,
        root,
        cnpg_kustomization,
        external_secrets_config_kustomization,
        gateway_kustomization,
        monitoring_crds_kustomization,
    )
    ollama_kustomization = ollama_flux_kustomizations.ollama(
        flux_chart,
        gateway_kustomization,
        cert_manager_environment_kustomization,
        nvidia_runtimeclass_kustomization,
        external_secrets_config_kustomization,
        reflector_kustomization,
        claude_rbac_kustomization,
    )
    seaweedfs_cluster_kustomization = seaweedfs_flux_kustomizations.seaweedfs_cluster(
        flux_chart,
        seaweedfs_operator_kustomization,
        seaweedfs_secrets_kustomization,
        seaweedfs_filer_db_kustomization,
        local_path_provisioner_kustomization,
    )
    atuin_flux_kustomizations.atuin_user_provisioner(flux_chart, atuin_kustomization, user_agentydragon_kustomization)
    agent_machine_access_tf_kustomization = agents_flux_kustomizations.agent_machine_access_tf(
        flux_chart, tofu_controller_kustomization, tofu_state_db_kustomization, authentik_kustomization
    )
    authentik_flux_kustomizations.authentik_proxy_routes(flux_chart, gateway_kustomization, authentik_kustomization)
    sso_providers_tf_kustomization = authentik_flux_kustomizations.sso_providers_tf(
        flux_chart, tofu_controller_kustomization, tofu_state_db_kustomization, authentik_kustomization
    )
    gatus_sso_tf_kustomization = gatus_flux_kustomizations.gatus_sso_tf(
        flux_chart, tofu_controller_kustomization, tofu_state_db_kustomization, authentik_kustomization
    )
    parked_flux_kustomizations.openhands(
        flux_chart,
        openhands_namespace_kustomization,
        openhands_sandboxes_kustomization,
        gateway_kustomization,
        authentik_kustomization,
        external_secrets_operator_kustomization,
    )
    parked_flux_kustomizations.tandoor(
        flux_chart,
        tandoor_namespace_kustomization,
        tandoor_db_kustomization,
        gateway_kustomization,
        authentik_kustomization,
    )
    flux_webhook_token_kustomization = flux_webhook_token_flux_kustomizations.flux_webhook_token(
        flux_chart,
        tofu_controller_kustomization,
        tofu_state_db_kustomization,
        github_secrets_sync_secrets_kustomization,
    )
    github_branch_protection_flux_kustomizations.github_branch_protection(
        flux_chart,
        tofu_controller_kustomization,
        tofu_state_db_kustomization,
        github_secrets_sync_secrets_kustomization,
    )
    monitoring_flux_kustomizations.monitoring_stack(
        flux_chart,
        monitoring_namespace_kustomization,
        monitoring_crds_kustomization,
        ntfy_kustomization,
        external_secrets_config_kustomization,
    )
    agents_flux_kustomizations.claude_sandbox_secrets(
        flux_chart, claude_rbac_kustomization, external_secrets_config_kustomization, ollama_kustomization
    )
    agents_flux_kustomizations.haku_openclaw_spike_backup(
        flux_chart,
        haku_openclaw_spike_namespace_kustomization,
        seaweedfs_cluster_kustomization,
        external_secrets_config_kustomization,
        volsync_kustomization,
    )
    authentik_flux_kustomizations.authentik_db_backups(
        flux_chart,
        authentik_db_kustomization,
        cnpg_kustomization,
        seaweedfs_cluster_kustomization,
        authentik_namespace_kustomization,
    )
    langfuse_seaweed_kustomization = langfuse_flux_kustomizations.langfuse_seaweed(
        flux_chart, langfuse_namespace_kustomization, seaweedfs_cluster_kustomization
    )
    langfuse_secrets_kustomization = langfuse_flux_kustomizations.langfuse_secrets(
        flux_chart, langfuse_namespace_kustomization, seaweedfs_cluster_kustomization
    )
    loki_kustomization = monitoring_flux_kustomizations.loki(
        flux_chart, grafana_helmrepository_kustomization, seaweedfs_cluster_kustomization
    )
    mimir_kustomization = monitoring_flux_kustomizations.mimir(
        flux_chart, monitoring_crds_kustomization, grafana_helmrepository_kustomization, seaweedfs_cluster_kustomization
    )
    monitoring_flux_kustomizations.tempo(
        flux_chart, monitoring_crds_kustomization, grafana_helmrepository_kustomization, seaweedfs_cluster_kustomization
    )
    seaweedfs_browsertrix_bucket_kustomization = parked_flux_kustomizations.seaweedfs_browsertrix_bucket(
        flux_chart, seaweedfs_cluster_kustomization, seaweedfs_secrets_kustomization
    )
    seaweedfs_drivefs_artifacts_bucket_kustomization = seaweedfs_flux_kustomizations.seaweedfs_drivefs_artifacts_bucket(
        flux_chart, seaweedfs_cluster_kustomization
    )
    seaweedfs_external_credentials_kustomization = seaweedfs_flux_kustomizations.seaweedfs_external_credentials(
        flux_chart, seaweedfs_secrets_kustomization, seaweedfs_cluster_kustomization
    )
    seaweedfs_flux_kustomizations.seaweedfs_forgejo_bucket(flux_chart, seaweedfs_cluster_kustomization)
    seaweedfs_flux_kustomizations.seaweedfs_langfuse_bucket(flux_chart, seaweedfs_cluster_kustomization)
    seaweedfs_flux_kustomizations.seaweedfs_loom_gym_bucket(flux_chart, seaweedfs_cluster_kustomization)
    seaweedfs_flux_kustomizations.seaweedfs_monitoring(
        flux_chart, seaweedfs_cluster_kustomization, monitoring_crds_kustomization
    )
    seaweedfs_pr_visuals_bucket_kustomization = seaweedfs_flux_kustomizations.seaweedfs_pr_visuals_bucket(
        flux_chart, seaweedfs_cluster_kustomization
    )
    seaweedfs_public_coder_agent_backups_bucket_kustomization = (
        seaweedfs_flux_kustomizations.seaweedfs_public_coder_agent_backups_bucket(
            flux_chart, seaweedfs_cluster_kustomization
        )
    )
    seaweedfs_registry_cache_bucket_kustomization = seaweedfs_flux_kustomizations.seaweedfs_registry_cache_bucket(
        flux_chart, seaweedfs_cluster_kustomization
    )
    seaweedfs_csi_kustomization = seaweedfs_csi_flux_kustomizations.seaweedfs_csi(
        flux_chart, seaweedfs_cluster_kustomization
    )
    vm_images_publisher_kustomization = vm_images_publisher_flux_kustomizations.vm_images_publisher(
        flux_chart, seaweedfs_cluster_kustomization
    )
    agents_flux_kustomizations.kubectl_passthrough_mcp(
        flux_chart, gateway_kustomization, agent_machine_access_tf_kustomization
    )
    kubectl_machine_mcp_kustomization = parked_flux_kustomizations.kubectl_machine_mcp(
        flux_chart, gateway_kustomization, agent_machine_access_tf_kustomization
    )
    forgejo_kustomization = forgejo_flux_kustomizations.forgejo(
        flux_chart,
        forgejo_namespace_kustomization,
        forgejo_db_kustomization,
        forgejo_cache_kustomization,
        gateway_kustomization,
        cert_manager_kustomization,
        seaweedfs_cluster_kustomization,
        reflector_kustomization,
        sso_providers_tf_kustomization,
        authentik_kustomization,
        monitoring_crds_kustomization,
    )
    headlamp_flux_kustomizations.headlamp(flux_chart, gateway_kustomization, sso_providers_tf_kustomization)
    matrix_kustomization = matrix_flux_kustomizations.matrix(
        flux_chart,
        matrix_namespace_kustomization,
        matrix_db_kustomization,
        sso_providers_tf_kustomization,
        reflector_kustomization,
        gateway_kustomization,
        local_path_provisioner_kustomization,
    )
    grafana_instance_kustomization = monitoring_flux_kustomizations.grafana_instance(
        flux_chart, grafana_operator_kustomization, grafana_db_kustomization, sso_providers_tf_kustomization
    )
    gatus_flux_kustomizations.gatus(
        flux_chart,
        gatus_namespace_kustomization,
        gatus_db_kustomization,
        gatus_sso_tf_kustomization,
        litellm_secrets_kustomization,
        gateway_kustomization,
        monitoring_crds_kustomization,
    )
    flux_webhook_flux_kustomizations.flux_webhook(
        flux_chart,
        flux_webhook_token_kustomization,
        ntfy_kustomization,
        external_secrets_config_kustomization,
        gateway_kustomization,
    )
    langfuse_flux_kustomizations.langfuse(
        flux_chart,
        langfuse_namespace_kustomization,
        langfuse_secrets_kustomization,
        langfuse_cache_kustomization,
        langfuse_db_kustomization,
        clickhouse_kustomization,
        langfuse_seaweed_kustomization,
        gateway_kustomization,
        claude_rbac_kustomization,
    )
    vector_talos_logs_flux_kustomizations.vector_talos_logs(flux_chart, loki_kustomization)
    monitoring_flux_kustomizations.alloy(flux_chart, mimir_kustomization, grafana_helmrepository_kustomization)
    agents_flux_kustomizations.public_coder_agent_backup(
        flux_chart,
        seaweedfs_public_coder_agent_backups_bucket_kustomization,
        external_secrets_config_kustomization,
        volsync_kustomization,
    )
    oci_cache_flux_kustomizations.oci_cache(
        flux_chart, valkey_kustomization, seaweedfs_registry_cache_bucket_kustomization, monitoring_crds_kustomization
    )
    parked_flux_kustomizations.archivebox(flux_chart, seaweedfs_csi_kustomization, local_path_provisioner_kustomization)
    parked_flux_kustomizations.browsertrix(
        flux_chart,
        browsertrix_namespace_kustomization,
        seaweedfs_browsertrix_bucket_kustomization,
        seaweedfs_secrets_kustomization,
        reflector_kustomization,
        local_path_provisioner_kustomization,
        seaweedfs_csi_kustomization,
    )
    seaweedfs_public_s3_kustomization = seaweedfs_flux_kustomizations.seaweedfs_public_s3(
        flux_chart,
        seaweedfs_external_credentials_kustomization,
        seaweedfs_drivefs_artifacts_bucket_kustomization,
        vm_images_publisher_kustomization,
        seaweedfs_secrets_kustomization,
        seaweedfs_cluster_kustomization,
        gateway_kustomization,
    )
    parked_flux_kustomizations.haku_cloud_agent(
        flux_chart,
        external_creds_kustomization,
        external_secrets_config_kustomization,
        tofu_controller_kustomization,
        tofu_state_db_kustomization,
        kubectl_machine_mcp_kustomization,
    )
    forgejo_agentydragon_repos_kustomization = forgejo_flux_kustomizations.forgejo_agentydragon_repos(
        flux_chart, forgejo_kustomization, tofu_controller_kustomization, tofu_state_db_kustomization
    )
    budget_ledger_kustomization = forgejo_flux_kustomizations.budget_ledger(
        flux_chart,
        forgejo_kustomization,
        tofu_controller_kustomization,
        tofu_state_db_kustomization,
        budget_namespace_kustomization,
    )
    forgejo_claude_kustomization = forgejo_flux_kustomizations.forgejo_claude(
        flux_chart,
        forgejo_kustomization,
        tofu_controller_kustomization,
        tofu_state_db_kustomization,
        claude_rbac_kustomization,
    )
    forgejo_images_kustomization = forgejo_images_flux_kustomizations.forgejo_images(
        flux_chart,
        external_secrets_config_kustomization,
        forgejo_kustomization,
        tofu_controller_kustomization,
        tofu_state_db_kustomization,
    )
    haku_ci_flux_kustomizations.haku_ci(
        flux_chart, forgejo_kustomization, keda_kustomization, reflector_kustomization, haku_forgejo_tea_kustomization
    )
    parked_flux_kustomizations.augur_evidence(
        flux_chart,
        forgejo_kustomization,
        tofu_controller_kustomization,
        tofu_state_db_kustomization,
        budget_namespace_kustomization,
    )
    flux_grafana_secrets_flux_kustomizations.flux_grafana_secrets(
        flux_chart, grafana_instance_kustomization, grafana_operator_kustomization
    )
    grafana_flux_kustomizations.clickhouse_grafana(flux_chart, clickhouse_kustomization, grafana_instance_kustomization)
    parked_flux_kustomizations.agent_box(
        flux_chart,
        kubevirt_kustomization,
        cdi_kustomization,
        external_secrets_operator_kustomization,
        seaweedfs_public_s3_kustomization,
        local_path_provisioner_kustomization,
    )
    parked_flux_kustomizations.gecko(
        flux_chart,
        gecko_namespace_kustomization,
        kubevirt_kustomization,
        cdi_kustomization,
        external_secrets_operator_kustomization,
        seaweedfs_public_s3_kustomization,
        local_path_provisioner_kustomization,
    )
    parked_flux_kustomizations.budget(
        flux_chart, budget_ledger_kustomization, gateway_kustomization, authentik_kustomization
    )
    activitywatch_flux_kustomizations.activitywatch(
        flux_chart,
        external_secrets_config_kustomization,
        forgejo_images_kustomization,
        local_path_provisioner_kustomization,
    )
    agentplane_index_kustomization = agentplane_index_flux_kustomizations.agentplane_index(
        flux_chart,
        cnpg_kustomization,
        external_secrets_config_kustomization,
        forgejo_images_kustomization,
        local_path_provisioner_kustomization,
        ollama_kustomization,
    )
    airlock_kustomization = agents_flux_kustomizations.airlock(
        flux_chart,
        forgejo_images_kustomization,
        gateway_kustomization,
        authentik_kustomization,
        external_secrets_config_kustomization,
    )
    authentik_jwt_rotation_kustomization = agents_flux_kustomizations.authentik_jwt_rotation(
        flux_chart,
        forgejo_images_kustomization,
        external_creds_kustomization,
        external_secrets_config_kustomization,
        agent_machine_access_tf_kustomization,
    )
    agents_flux_kustomizations.loki_read_proxy(
        flux_chart, external_secrets_config_kustomization, forgejo_images_kustomization
    )
    agents_flux_kustomizations.plaid_mcp(
        flux_chart,
        forgejo_images_kustomization,
        gateway_kustomization,
        cnpg_kustomization,
        local_path_provisioner_kustomization,
        external_secrets_config_kustomization,
        valkey_kustomization,
        agent_machine_access_tf_kustomization,
        reflector_kustomization,
        monitoring_crds_kustomization,
    )
    tana_mcp_kustomization = agents_flux_kustomizations.tana_mcp(
        flux_chart,
        external_secrets_config_kustomization,
        forgejo_images_kustomization,
        gateway_kustomization,
        valkey_kustomization,
        agent_machine_access_tf_kustomization,
        reflector_kustomization,
        monitoring_crds_kustomization,
    )
    cli_proxy_api_kustomization = cli_proxy_api_flux_kustomizations.cli_proxy_api(
        flux_chart,
        external_secrets_config_kustomization,
        gateway_kustomization,
        cert_manager_environment_kustomization,
        sso_providers_tf_kustomization,
        forgejo_images_kustomization,
    )
    cpap_sync_kustomization = cpap_sync_flux_kustomizations.cpap_sync(
        flux_chart, external_secrets_config_kustomization, kubevirt_kustomization, forgejo_images_kustomization
    )
    flux_image_automation_forgejo_kustomization = (
        flux_image_automation_forgejo_flux_kustomizations.flux_image_automation_forgejo(
            flux_chart, forgejo_images_kustomization, flux_image_automation_ghcr_kustomization
        )
    )
    github_api_proxy_flux_kustomizations.github_api_proxy(
        flux_chart,
        external_secrets_config_kustomization,
        github_api_proxy_identity_kustomization,
        forgejo_images_kustomization,
        seaweedfs_csi_kustomization,
        monitoring_crds_kustomization,
        gateway_kustomization,
        reloader_kustomization,
    )
    github_exporter_flux_kustomizations.github_exporter(
        flux_chart,
        forgejo_images_kustomization,
        monitoring_namespace_kustomization,
        monitoring_crds_kustomization,
        grafana_instance_kustomization,
        external_secrets_config_kustomization,
        external_creds_kustomization,
    )
    github_secrets_sync_flux_kustomizations.github_secrets_sync(
        flux_chart,
        tofu_controller_kustomization,
        tofu_state_db_kustomization,
        github_secrets_sync_secrets_kustomization,
        forgejo_images_kustomization,
        seaweedfs_pr_visuals_bucket_kustomization,
    )
    grocy_sf_kustomization = grocy_flux_kustomizations.grocy_sf(
        flux_chart,
        forgejo_images_kustomization,
        gateway_kustomization,
        cert_manager_issuer_config_kustomization,
        cert_manager_environment_kustomization,
        authentik_kustomization,
        volsync_kustomization,
    )
    grocy_vallejo_kustomization = grocy_flux_kustomizations.grocy_vallejo(
        flux_chart,
        forgejo_images_kustomization,
        gateway_kustomization,
        cert_manager_issuer_config_kustomization,
        cert_manager_environment_kustomization,
        authentik_kustomization,
        volsync_kustomization,
    )
    haku_flux_kustomizations.haku_mailbox(
        flux_chart,
        forgejo_images_kustomization,
        haku_mailbox_namespace_kustomization,
        haku_mailbox_db_kustomization,
        cert_manager_kustomization,
        agent_machine_access_tf_kustomization,
        gateway_kustomization,
        external_secrets_config_kustomization,
        cert_manager_issuer_config_kustomization,
    )
    home_assistant_kustomization = home_assistant_flux_kustomizations.home_assistant(
        flux_chart,
        local_path_provisioner_kustomization,
        seaweedfs_cluster_kustomization,
        volsync_kustomization,
        external_secrets_config_kustomization,
        forgejo_images_kustomization,
        monitoring_crds_kustomization,
        gateway_kustomization,
        sso_providers_tf_kustomization,
    )
    matrix_flux_kustomizations.matrix_user_provisioner(
        flux_chart, external_secrets_config_kustomization, forgejo_images_kustomization, matrix_kustomization
    )
    nix_cache_flux_kustomizations.nix_cache(
        flux_chart,
        cnpg_kustomization,
        external_creds_kustomization,
        external_secrets_config_kustomization,
        forgejo_images_kustomization,
        gateway_kustomization,
        cert_manager_kustomization,
        seaweedfs_cluster_kustomization,
    )
    inventree_kustomization = parked_flux_kustomizations.inventree(
        flux_chart,
        forgejo_images_kustomization,
        inventree_namespace_kustomization,
        inventree_db_kustomization,
        sso_providers_tf_kustomization,
        reflector_kustomization,
        gateway_kustomization,
        authentik_kustomization,
    )
    parked_flux_kustomizations.manifold_mcp(
        flux_chart,
        external_secrets_config_kustomization,
        forgejo_images_kustomization,
        gateway_kustomization,
        valkey_kustomization,
        agent_machine_access_tf_kustomization,
        reflector_kustomization,
        monitoring_crds_kustomization,
    )
    parked_flux_kustomizations.osm_mcp(flux_chart, external_secrets_config_kustomization, forgejo_images_kustomization)
    parked_flux_kustomizations.postscanmail_mcp(
        flux_chart,
        external_secrets_config_kustomization,
        forgejo_images_kustomization,
        gateway_kustomization,
        valkey_kustomization,
        agent_machine_access_tf_kustomization,
        reflector_kustomization,
        monitoring_crds_kustomization,
    )
    parked_flux_kustomizations.sdr(
        flux_chart,
        external_secrets_config_kustomization,
        forgejo_images_kustomization,
        gateway_kustomization,
        authentik_kustomization,
    )
    ssh_mcp_kustomization = ssh_mcp_generation.ssh_mcp(
        flux_chart,
        root,
        mesh,
        devbox_service,
        ssh_mcp_namespace_kustomization,
        external_secrets_config_kustomization,
        forgejo_images_kustomization,
    )
    study_casino_flux_kustomizations.study_casino(
        flux_chart,
        external_secrets_config_kustomization,
        forgejo_images_kustomization,
        gateway_kustomization,
        study_casino_namespace_kustomization,
        study_casino_db_kustomization,
        claude_rbac_kustomization,
    )
    haku_state_kustomization = forgejo_flux_kustomizations.haku_state(
        flux_chart,
        forgejo_kustomization,
        tofu_controller_kustomization,
        tofu_state_db_kustomization,
        agentplane_index_kustomization,
        haku_namespace_kustomization,
        haku_console_namespace_kustomization,
        haku_egress_proxy_namespace_kustomization,
    )
    parked_flux_kustomizations.google_workspace_mcp(
        flux_chart, airlock_kustomization, local_path_provisioner_kustomization, reflector_kustomization
    )
    monitoring_flux_kustomizations.alloy_otlp_bearer_token_tf(
        flux_chart, tofu_controller_kustomization, tofu_state_db_kustomization, authentik_jwt_rotation_kustomization
    )
    litellm_kustomization = litellm_proxy.litellm(
        flux_chart,
        root,
        external_secrets_config_kustomization,
        forgejo_images_kustomization,
        litellm_secrets_kustomization,
        litellm_db_kustomization,
        gateway_kustomization,
        cert_manager_environment_kustomization,
        langfuse_secrets_kustomization,
        reflector_kustomization,
        tana_mcp_kustomization,
        monitoring_crds_kustomization,
    )
    aiquota_kustomization = aiquota.aiquota(
        flux_chart,
        root,
        external_secrets_config_kustomization,
        forgejo_images_kustomization,
        cli_proxy_api_kustomization,
        external_secrets_operator_kustomization,
        clickhouse_schema_kustomization,
        agent_machine_access_tf_kustomization,
        reflector_kustomization,
    )
    forgejo_flux_kustomizations.cpap_data(
        flux_chart,
        forgejo_kustomization,
        tofu_controller_kustomization,
        tofu_state_db_kustomization,
        cpap_sync_kustomization,
    )
    grocy_flux_kustomizations.grocy_mcp_sf(
        flux_chart,
        external_secrets_config_kustomization,
        forgejo_images_kustomization,
        gateway_kustomization,
        grocy_sf_kustomization,
        valkey_kustomization,
        agent_machine_access_tf_kustomization,
        reflector_kustomization,
        monitoring_crds_kustomization,
    )
    grocy_flux_kustomizations.grocy_sf_user_perms(flux_chart, forgejo_images_kustomization, grocy_sf_kustomization)
    grocy_flux_kustomizations.grocy_mcp_vallejo(
        flux_chart,
        external_secrets_config_kustomization,
        forgejo_images_kustomization,
        gateway_kustomization,
        grocy_vallejo_kustomization,
        valkey_kustomization,
        agent_machine_access_tf_kustomization,
        reflector_kustomization,
        monitoring_crds_kustomization,
    )
    grocy_flux_kustomizations.grocy_vallejo_user_perms(
        flux_chart, forgejo_images_kustomization, grocy_vallejo_kustomization
    )
    ha_mcp_kustomization = ha_mcp.ha_mcp(
        flux_chart,
        root,
        external_secrets_config_kustomization,
        forgejo_images_kustomization,
        home_assistant_kustomization,
        monitoring_crds_kustomization,
    )
    parked_flux_kustomizations.inventree_token_provisioner(
        flux_chart,
        external_secrets_config_kustomization,
        forgejo_images_kustomization,
        inventree_kustomization,
        claude_rbac_kustomization,
    )
    agents_flux_kustomizations.forgejo_token_rotation(
        flux_chart,
        forgejo_images_kustomization,
        authentik_jwt_rotation_kustomization,
        forgejo_claude_kustomization,
        haku_state_kustomization,
        forgejo_agentydragon_repos_kustomization,
    )
    haku_egress_proxy_kustomization = agents_flux_kustomizations.haku_egress_proxy(
        flux_chart,
        haku_egress_proxy_namespace_kustomization,
        haku_openclaw_spike_namespace_kustomization,
        haku_state_kustomization,
        cert_manager_environment_kustomization,
        cert_manager_trust_kustomization,
        reflector_kustomization,
        external_secrets_config_kustomization,
        external_creds_kustomization,
        forgejo_images_kustomization,
    )
    haku_flux_kustomizations.haku_ui_image_webhook(flux_chart, haku_state_kustomization)
    haku_flux_kustomizations.haku_workloads(flux_chart, haku_state_kustomization)
    litellm_keys_tf_kustomization = litellm_flux_kustomizations.litellm_keys_tf(
        flux_chart, litellm_kustomization, tofu_controller_kustomization, tofu_state_db_kustomization
    )
    agents_flux_kustomizations.haku_openclaw_spike_app(
        flux_chart,
        external_secrets_config_kustomization,
        haku_openclaw_spike_namespace_kustomization,
        haku_egress_proxy_kustomization,
        forgejo_images_kustomization,
        flux_image_automation_forgejo_kustomization,
        seaweedfs_cluster_kustomization,
    )
    haku_workspaces_kustomization = haku_flux_kustomizations.haku_workspaces(
        flux_chart,
        agent_sandbox_controller_kustomization,
        haku_rbac_kustomization,
        haku_egress_proxy_kustomization,
        kyverno_policies_kustomization,
        external_secrets_config_kustomization,
        forgejo_images_kustomization,
    )
    parked_flux_kustomizations.haku_managed_agent(
        flux_chart,
        forgejo_images_kustomization,
        agent_shared_secrets_kustomization,
        external_secrets_config_kustomization,
        haku_namespace_kustomization,
        haku_rbac_kustomization,
        haku_state_kustomization,
        haku_egress_proxy_kustomization,
    )
    testing.agentplane_testing(
        flux_chart,
        agentplane_testing_health_checks,
        agentplane_crds_kustomization,
        agent_sandbox_controller_kustomization,
        cert_manager_environment_kustomization,
        cert_manager_trust_kustomization,
        claude_rbac_kustomization,
        cnpg_kustomization,
        external_creds_kustomization,
        external_secrets_config_kustomization,
        forgejo_images_kustomization,
        gateway_kustomization,
        litellm_keys_tf_kustomization,
        local_path_provisioner_kustomization,
        reflector_kustomization,
    )
    agents_flux_kustomizations.agent_workspaces_app(
        flux_chart,
        external_secrets_config_kustomization,
        forgejo_images_kustomization,
        agent_sandbox_controller_kustomization,
        litellm_keys_tf_kustomization,
        kyverno_policies_kustomization,
    )
    public_coder_agent_proxy_kustomization = agents_flux_kustomizations.public_coder_agent_proxy(
        flux_chart,
        external_secrets_config_kustomization,
        public_coder_agent_namespace_kustomization,
        cert_manager_environment_kustomization,
        cert_manager_trust_kustomization,
        reflector_kustomization,
        forgejo_images_kustomization,
        agent_machine_access_tf_kustomization,
        matrix_kustomization,
        aiquota_kustomization,
        litellm_keys_tf_kustomization,
    )
    parked_flux_kustomizations.haku_dispatch(
        flux_chart,
        cnpg_kustomization,
        local_path_provisioner_kustomization,
        external_secrets_config_kustomization,
        external_secrets_operator_kustomization,
        litellm_kustomization,
        litellm_keys_tf_kustomization,
    )
    haku_console_kustomization = haku_charts.haku_console(
        flux_chart,
        haku_console_health_checks,
        haku_workspaces_kustomization,
        haku_console_namespace_kustomization,
        cnpg_kustomization,
        local_path_provisioner_kustomization,
        forgejo_images_kustomization,
        haku_state_kustomization,
        gateway_kustomization,
        agent_machine_access_tf_kustomization,
        reflector_kustomization,
        external_creds_kustomization,
        external_secrets_config_kustomization,
        ssh_mcp_kustomization,
        monitoring_crds_kustomization,
    )
    agents_flux_kustomizations.public_coder_agent_app(
        flux_chart,
        public_coder_agent_namespace_kustomization,
        public_coder_agent_proxy_kustomization,
        external_secrets_config_kustomization,
        external_creds_kustomization,
        litellm_keys_tf_kustomization,
    )
    agents_flux_kustomizations.public_coder_agent_devbox(
        flux_chart,
        public_coder_agent_namespace_kustomization,
        public_coder_agent_proxy_kustomization,
        kubevirt_kustomization,
        forgejo_images_kustomization,
    )
    staging.agentplane_staging(
        flux_chart,
        agentplane_staging_health_checks,
        agentplane_crds_kustomization,
        agent_sandbox_controller_kustomization,
        cert_manager_environment_kustomization,
        cert_manager_trust_kustomization,
        claude_rbac_kustomization,
        cnpg_kustomization,
        external_creds_kustomization,
        external_secrets_config_kustomization,
        forgejo_images_kustomization,
        gateway_kustomization,
        litellm_keys_tf_kustomization,
        local_path_provisioner_kustomization,
        reflector_kustomization,
        sso_providers_tf_kustomization,
        ssh_mcp_kustomization,
        haku_console_kustomization,
        ha_mcp_kustomization,
    )
    flux_app.synth()


def main() -> None:
    generate_manifests(get_build_workspace_directory())


if __name__ == "__main__":
    main()
