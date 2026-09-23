"""Dispatch manifest generation to each component's local cdk8s helpers."""

from pathlib import Path

from cdk8s import App, Chart

from cluster.cdk8s import (
    agent_machine_access,
    aiquota,
    cnpg_flux_kustomizations,
    descheduler,
    dns_automation,
    dns_automation_flux_kustomizations,
    egress_fences,
    etcd,
    external_creds,
    forgejo_image_automation,
    forgejo_images,
    forgejo_images_flux_kustomizations,
    gateway_flux_kustomizations,
    github_branch_protection,
    goldilocks,
    google_mcp,
    ha_mcp,
    haku_openclaw_spike_config,
    hubble_ui,
    keda,
    kube_system,
    local_path_provisioner,
    metrics_server,
    ntfy,
    nvidia_runtimeclass,
    public_coder_agent_config,
    public_coder_devbox,
    reflector,
    stateful_infra,
    user_agentydragon,
    valkey,
)
from cluster.cdk8s.activitywatch import flux_kustomizations as activitywatch_flux_kustomizations
from cluster.cdk8s.agentplane import generation as agentplane_generation, staging, testing
from cluster.cdk8s.agentplane_crds import flux_kustomizations as agentplane_crds_flux_kustomizations
from cluster.cdk8s.agentplane_index import flux_kustomizations as agentplane_index_flux_kustomizations
from cluster.cdk8s.agents import flux_kustomizations as agents_flux_kustomizations, namespaces as agents_namespaces
from cluster.cdk8s.artifact_generators import (
    artifact,
    artifact_generators as artifact_generators_factory,
    write_artifact_generators,
)
from cluster.cdk8s.atuin import flux_kustomizations as atuin_flux_kustomizations
from cluster.cdk8s.authentik import (
    db as authentik_db,
    flux_kustomizations as authentik_flux_kustomizations,
    namespace as authentik_namespace,
    sso_providers,
)
from cluster.cdk8s.cert_manager import flux_kustomizations as cert_manager_flux_kustomizations
from cluster.cdk8s.cli_proxy_api import flux_kustomizations as cli_proxy_api_flux_kustomizations
from cluster.cdk8s.clickhouse import (
    flux_kustomizations as clickhouse_flux_kustomizations,
    namespace as clickhouse_namespace,
    schema as clickhouse_schema,
)
from cluster.cdk8s.coredns_custom import flux_kustomizations as coredns_custom_flux_kustomizations
from cluster.cdk8s.cpap_sync import flux_kustomizations as cpap_sync_flux_kustomizations
from cluster.cdk8s.dcgm_exporter import flux_kustomizations as dcgm_exporter_flux_kustomizations
from cluster.cdk8s.external_secrets import flux_kustomizations as external_secrets_flux_kustomizations
from cluster.cdk8s.flux import health_checks as flux_health_checks
from cluster.cdk8s.flux_grafana_secrets import flux_kustomizations as flux_grafana_secrets_flux_kustomizations
from cluster.cdk8s.flux_image_automation_ghcr import (
    flux_kustomizations as flux_image_automation_ghcr_flux_kustomizations,
)
from cluster.cdk8s.flux_monitoring import flux_kustomizations as flux_monitoring_flux_kustomizations
from cluster.cdk8s.flux_webhook import flux_kustomizations as flux_webhook_flux_kustomizations
from cluster.cdk8s.flux_webhook_token import flux_webhook_token
from cluster.cdk8s.forgejo import (
    db as forgejo_db,
    flux_kustomizations as forgejo_flux_kustomizations,
    gitops_modules as forgejo_gitops_modules,
    namespace as forgejo_namespace,
)
from cluster.cdk8s.gaffer_private_source import flux_kustomizations as gaffer_private_source_flux_kustomizations
from cluster.cdk8s.gatus import flux_kustomizations as gatus_flux_kustomizations, sso as gatus_sso
from cluster.cdk8s.github_api_proxy import flux_kustomizations as github_api_proxy_flux_kustomizations
from cluster.cdk8s.github_exporter import flux_kustomizations as github_exporter_flux_kustomizations
from cluster.cdk8s.github_secrets_sync import (
    flux_kustomizations as github_secrets_sync_flux_kustomizations,
    gitops_module as github_secrets_sync_gitops_module,
)
from cluster.cdk8s.grafana import flux_kustomizations as grafana_flux_kustomizations
from cluster.cdk8s.grocy import flux_kustomizations as grocy_flux_kustomizations
from cluster.cdk8s.haku import charts as haku_charts, flux_kustomizations as haku_flux_kustomizations
from cluster.cdk8s.haku_ci import flux_kustomizations as haku_ci_flux_kustomizations
from cluster.cdk8s.headlamp import flux_kustomizations as headlamp_flux_kustomizations
from cluster.cdk8s.home_assistant import (
    flux_kustomizations as home_assistant_flux_kustomizations,
    namespace as home_assistant_namespace,
)
from cluster.cdk8s.infra_drift import drift_watch, flux_kustomizations as infra_drift_flux_kustomizations
from cluster.cdk8s.kube_api_proxy import flux_kustomizations as kube_api_proxy_flux_kustomizations
from cluster.cdk8s.kubevirt import flux_kustomizations as kubevirt_flux_kustomizations
from cluster.cdk8s.kyverno import flux_kustomizations as kyverno_flux_kustomizations
from cluster.cdk8s.langfuse import flux_kustomizations as langfuse_flux_kustomizations
from cluster.cdk8s.litellm import (
    credentials as litellm_credentials,
    keys as litellm_keys,
    namespace as litellm_namespace,
    proxy as litellm_proxy,
)
from cluster.cdk8s.matrix import flux_kustomizations as matrix_flux_kustomizations
from cluster.cdk8s.monitoring import alloy_otlp_bearer_token, flux_kustomizations as monitoring_flux_kustomizations
from cluster.cdk8s.nix_cache import flux_kustomizations as nix_cache_flux_kustomizations
from cluster.cdk8s.node_feature_discovery import flux_kustomizations as node_feature_discovery_flux_kustomizations
from cluster.cdk8s.nvidia_device_plugin import flux_kustomizations as nvidia_device_plugin_flux_kustomizations
from cluster.cdk8s.oci_cache import flux_kustomizations as oci_cache_flux_kustomizations
from cluster.cdk8s.ollama import flux_kustomizations as ollama_flux_kustomizations
from cluster.cdk8s.openebs_lvm import flux_kustomizations as openebs_lvm_flux_kustomizations
from cluster.cdk8s.parked import flux_kustomizations as parked_flux_kustomizations
from cluster.cdk8s.proxmox_proxy import flux_kustomizations as proxmox_proxy_flux_kustomizations
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
from cluster.cdk8s.tofu_state import db as tofu_state_db, namespace as tofu_state_namespace
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
        root, testing.ENV, testing.chart, write_kustomization=False
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
    litellm_namespace.write_manifests(root)
    forgejo_image_automation.write_manifests(root)
    agents_namespaces.write_manifests(root)
    tofu_state_namespace.write_manifests(root)
    tofu_state_db.write_manifests(root)
    authentik_namespace.write_manifests(root)
    authentik_db.write_manifests(root)
    clickhouse_namespace.write_manifests(root)
    forgejo_namespace.write_manifests(root)
    forgejo_db.write_manifests(root)
    home_assistant_namespace.write_manifests(root)
    github_branch_protection.write_manifests(root)
    agent_machine_access.write_manifests(root)
    forgejo_gitops_modules.write_manifests(root)
    alloy_otlp_bearer_token.write_manifests(root)
    drift_watch.write_manifests(root)
    github_secrets_sync_gitops_module.write_manifests(root)
    forgejo_images.write_manifests(root)
    gatus_sso.write_manifests(root)
    flux_webhook_token.write_manifests(root)
    sso_providers.write_manifests(root)
    litellm_credentials.write_agentplane_testing_manifests(root)
    kube_system.write_manifests(root)
    user_agentydragon.write_manifests(root)
    nvidia_runtimeclass.write_manifests(root)
    hubble_ui.write_manifests(root)
    metrics_server.write_manifests(root)
    reflector.write_manifests(root)
    keda.write_manifests(root)
    valkey.write_manifests(root)
    local_path_provisioner.write_manifests(root)
    goldilocks.write_manifests(root)

    flux_output = root / "cluster/k8s/flux"
    flux_output.mkdir(parents=True, exist_ok=True)
    flux_app = App(outdir=str(flux_output))
    flux_chart = Chart(flux_app, "kustomizations", disable_resource_name_hashes=True)
    agentplane_crds_artifact = artifact("agentplane-crds", "cluster/k8s/agentplane-crds")
    agentplane_crds_kustomization = agentplane_crds_flux_kustomizations.agentplane_crds(
        flux_chart, agentplane_crds_artifact
    )
    agent_sandbox_controller_artifact = artifact(
        "agent-sandbox-controller", "cluster/k8s/agents/agent-sandbox/controller"
    )
    agent_sandbox_controller_kustomization = agents_flux_kustomizations.agent_sandbox_controller(
        flux_chart, agent_sandbox_controller_artifact
    )
    artifact_generators_factory(flux_chart)
    cert_manager_issuer_config_artifact = artifact(
        "cert-manager-issuer-config", "cluster/k8s/cert-manager/issuer-config"
    )
    cert_manager_issuer_config_kustomization = cert_manager_flux_kustomizations.cert_manager_issuer_config(
        flux_chart, cert_manager_issuer_config_artifact
    )
    coredns_custom_artifact = artifact("coredns-custom", "cluster/k8s/coredns-custom")
    coredns_custom_flux_kustomizations.coredns_custom(flux_chart, coredns_custom_artifact)
    external_secrets_crds_kustomization = external_secrets_flux_kustomizations.external_secrets_crds(flux_chart)
    flux_image_automation_ghcr_artifact = artifact(
        "flux-image-automation-ghcr", "cluster/k8s/flux-image-automation-ghcr"
    )
    flux_image_automation_ghcr_kustomization = (
        flux_image_automation_ghcr_flux_kustomizations.flux_image_automation_ghcr(
            flux_chart, flux_image_automation_ghcr_artifact
        )
    )
    budget_namespace_artifact = artifact("budget-namespace", "cluster/k8s/forgejo/budget-namespace")
    budget_namespace_kustomization = forgejo_flux_kustomizations.budget_namespace(flux_chart, budget_namespace_artifact)
    haku_namespace_artifact = artifact("haku-namespace", "cluster/k8s/haku/namespace")
    haku_namespace_kustomization = haku_flux_kustomizations.haku_namespace(flux_chart, haku_namespace_artifact)
    hubble_ui_artifact = artifact("hubble-ui", hubble_ui.OUTPUT_DIR)
    hubble_ui.hubble_ui(flux_chart, hubble_ui_artifact)
    kube_api_proxy_artifact = artifact("kube-api-proxy", "cluster/k8s/kube-api-proxy")
    kube_api_proxy_flux_kustomizations.kube_api_proxy(flux_chart, kube_api_proxy_artifact)
    kubevirt_cdi_operator_artifact = artifact("kubevirt-cdi-operator", "cluster/k8s/kubevirt/cdi-operator")
    cdi_operator_kustomization = kubevirt_flux_kustomizations.cdi_operator(flux_chart, kubevirt_cdi_operator_artifact)
    kubevirt_operator_artifact = artifact("kubevirt-operator", "cluster/k8s/kubevirt/operator")
    kubevirt_operator_kustomization = kubevirt_flux_kustomizations.kubevirt_operator(
        flux_chart, kubevirt_operator_artifact
    )
    kyverno_artifact = artifact("kyverno", "cluster/k8s/kyverno/app")
    kyverno_kustomization = kyverno_flux_kustomizations.kyverno(flux_chart, kyverno_artifact)
    local_path_provisioner_artifact = artifact("local-path-provisioner", local_path_provisioner.OUTPUT_DIR)
    local_path_provisioner_kustomization = local_path_provisioner.local_path_provisioner(
        flux_chart, local_path_provisioner_artifact
    )
    monitoring_crds_kustomization = monitoring_flux_kustomizations.monitoring_crds(flux_chart)
    grafana_helmrepository_artifact = artifact(
        "grafana-helmrepository", "cluster/k8s/monitoring/grafana-helmrepository"
    )
    grafana_helmrepository_kustomization = monitoring_flux_kustomizations.grafana_helmrepository(
        flux_chart, grafana_helmrepository_artifact
    )
    monitoring_namespace_artifact = artifact("monitoring-namespace", "cluster/k8s/monitoring/namespace")
    monitoring_namespace_kustomization = monitoring_flux_kustomizations.monitoring_namespace(
        flux_chart, monitoring_namespace_artifact
    )
    node_feature_discovery_artifact = artifact("node-feature-discovery", "cluster/k8s/node-feature-discovery")
    node_feature_discovery_kustomization = node_feature_discovery_flux_kustomizations.node_feature_discovery(
        flux_chart, node_feature_discovery_artifact
    )
    nvidia_runtimeclass_artifact = artifact("nvidia-runtimeclass", nvidia_runtimeclass.OUTPUT_DIR)
    nvidia_runtimeclass_kustomization = nvidia_runtimeclass.nvidia_runtimeclass(
        flux_chart, nvidia_runtimeclass_artifact
    )
    openebs_lvm_artifact = artifact("openebs-lvm", "cluster/k8s/openebs-lvm")
    openebs_lvm_flux_kustomizations.openebs_lvm(flux_chart, openebs_lvm_artifact)
    parked_flux_kustomizations.buildbuddy_executor(flux_chart)
    gecko_namespace_artifact = artifact("gecko-namespace", "cluster/k8s/parked/gecko/namespace")
    gecko_namespace_kustomization = parked_flux_kustomizations.gecko_namespace(flux_chart, gecko_namespace_artifact)
    reflector_artifact = artifact("reflector", reflector.OUTPUT_DIR)
    reflector_kustomization = reflector.reflector(flux_chart, reflector_artifact)
    seaweedfs_namespace_artifact = artifact("seaweedfs-namespace", "cluster/k8s/seaweedfs/namespace")
    seaweedfs_namespace_kustomization = seaweedfs_flux_kustomizations.seaweedfs_namespace(
        flux_chart, seaweedfs_namespace_artifact
    )
    snapshot_controller_crds_kustomization = snapshot_controller_flux_kustomizations.snapshot_controller_crds(
        flux_chart
    )
    sshpiper_crds_kustomization = sshpiper_crds_flux_kustomizations.sshpiper_crds(flux_chart)
    talos_cloud_controller_manager_artifact = artifact(
        "talos-cloud-controller-manager", "cluster/k8s/talos-cloud-controller-manager"
    )
    (
        talos_cloud_controller_manager_flux_kustomizations.talos_cloud_controller_manager(
            flux_chart, talos_cloud_controller_manager_artifact
        )
    )
    user_agentydragon_artifact = artifact("user-agentydragon", user_agentydragon.OUTPUT_DIR)
    user_agentydragon_kustomization = user_agentydragon.user_agentydragon(flux_chart, user_agentydragon_artifact)
    valkey_artifact = artifact("valkey", valkey.OUTPUT_DIR)
    valkey_kustomization = valkey.valkey(flux_chart, valkey_artifact)
    gaffer_private_source_flux_kustomizations.gaffer_private_source(
        flux_chart, flux_image_automation_ghcr_kustomization
    )
    haku_rbac_artifact = artifact("haku-rbac", "cluster/k8s/haku/rbac")
    haku_rbac_kustomization = haku_flux_kustomizations.haku_rbac(
        flux_chart, haku_rbac_artifact, haku_namespace_kustomization
    )
    kubevirt_artifact = artifact("kubevirt", "cluster/k8s/kubevirt/app")
    kubevirt_kustomization = kubevirt_flux_kustomizations.kubevirt(
        flux_chart, kubevirt_artifact, kubevirt_operator_kustomization
    )
    descheduler_artifact = artifact("descheduler", descheduler.OUTPUT_DIR)
    descheduler.descheduler(flux_chart, descheduler_artifact, kyverno_kustomization)
    keda_artifact = artifact("keda", keda.OUTPUT_DIR)
    keda_kustomization = keda.keda(flux_chart, keda_artifact, kyverno_kustomization)
    kyverno_policies_artifact = artifact("kyverno-policies", "cluster/k8s/kyverno/policies")
    kyverno_policies_kustomization = kyverno_flux_kustomizations.kyverno_policies(
        flux_chart, kyverno_policies_artifact, kyverno_kustomization
    )
    metrics_server_artifact = artifact("metrics-server", metrics_server.OUTPUT_DIR)
    metrics_server_kustomization = metrics_server.metrics_server(
        flux_chart, metrics_server_artifact, kyverno_kustomization
    )
    reloader_artifact = artifact("reloader", "cluster/k8s/reloader")
    reloader_flux_kustomizations.reloader(flux_chart, reloader_artifact, kyverno_kustomization)
    cdi_artifact = artifact("cdi", "cluster/k8s/kubevirt/cdi")
    cdi_kustomization = kubevirt_flux_kustomizations.cdi(
        flux_chart, cdi_artifact, cdi_operator_kustomization, local_path_provisioner_kustomization
    )
    clickhouse_operator_artifact = artifact("clickhouse-operator", "cluster/k8s/clickhouse/operator")
    clickhouse_operator_kustomization = clickhouse_flux_kustomizations.clickhouse_operator(
        flux_chart, clickhouse_operator_artifact, monitoring_crds_kustomization
    )
    flux_monitoring_artifact = artifact("flux-monitoring", "cluster/k8s/flux-monitoring")
    flux_monitoring_flux_kustomizations.flux_monitoring(
        flux_chart, flux_monitoring_artifact, monitoring_crds_kustomization
    )
    monitoring_cilium_artifact = artifact("monitoring-cilium", "cluster/k8s/monitoring/cilium")
    monitoring_flux_kustomizations.cilium_monitoring(
        flux_chart, monitoring_cilium_artifact, monitoring_crds_kustomization
    )
    monitoring_etcd_artifact = artifact("monitoring-etcd", etcd.OUTPUT_DIR)
    etcd.etcd_monitoring(flux_chart, monitoring_etcd_artifact, root, mesh, monitoring_crds_kustomization)
    monitoring_rules_artifact = artifact("monitoring-rules", "cluster/k8s/monitoring/rules")
    monitoring_flux_kustomizations.monitoring_rules(
        flux_chart, monitoring_rules_artifact, monitoring_crds_kustomization
    )
    grafana_operator_artifact = artifact("grafana-operator", "cluster/k8s/monitoring/grafana-operator")
    grafana_operator_kustomization = monitoring_flux_kustomizations.grafana_operator(
        flux_chart, grafana_operator_artifact, monitoring_namespace_kustomization
    )
    nvidia_device_plugin_artifact = artifact("nvidia-device-plugin", "cluster/k8s/nvidia-device-plugin")
    nvidia_device_plugin_kustomization = nvidia_device_plugin_flux_kustomizations.nvidia_device_plugin(
        flux_chart,
        nvidia_device_plugin_artifact,
        nvidia_runtimeclass_kustomization,
        node_feature_discovery_kustomization,
    )
    cert_manager_artifact = artifact("cert-manager", "cluster/k8s/cert-manager/app")
    cert_manager_kustomization = cert_manager_flux_kustomizations.cert_manager(
        flux_chart,
        cert_manager_artifact,
        cert_manager_issuer_config_kustomization,
        reflector_kustomization,
        monitoring_crds_kustomization,
    )
    seaweedfs_operator_artifact = artifact("seaweedfs-operator", "cluster/k8s/seaweedfs/operator")
    seaweedfs_operator_kustomization = seaweedfs_flux_kustomizations.seaweedfs_operator(
        flux_chart, seaweedfs_operator_artifact, seaweedfs_namespace_kustomization
    )
    snapshot_controller_kustomization = snapshot_controller_flux_kustomizations.snapshot_controller(
        flux_chart, snapshot_controller_crds_kustomization
    )
    forgejo_cache_artifact = artifact("forgejo-cache", "cluster/k8s/forgejo/cache")
    forgejo_flux_kustomizations.forgejo_cache(
        flux_chart, forgejo_cache_artifact, valkey_kustomization, local_path_provisioner_kustomization
    )
    haku_forgejo_tea_artifact = artifact("haku-forgejo-tea", "cluster/k8s/haku/forgejo-tea")
    haku_flux_kustomizations.haku_forgejo_tea(flux_chart, haku_forgejo_tea_artifact, haku_rbac_kustomization)
    claude_rbac_artifact = artifact("claude-rbac", "cluster/k8s/agents/agent-rbac-base")
    claude_rbac_kustomization = agents_flux_kustomizations.claude_rbac(
        flux_chart, claude_rbac_artifact, kyverno_policies_kustomization
    )
    vpa_artifact = artifact("vpa", "cluster/k8s/vpa")
    vpa_kustomization = vpa_flux_kustomizations.vpa(
        flux_chart, vpa_artifact, kyverno_kustomization, metrics_server_kustomization
    )
    clickhouse_artifact = artifact("clickhouse", "cluster/k8s/clickhouse/cluster")
    clickhouse_kustomization = clickhouse_flux_kustomizations.clickhouse(
        flux_chart, clickhouse_artifact, clickhouse_operator_kustomization
    )
    dcgm_exporter_artifact = artifact("dcgm-exporter", "cluster/k8s/dcgm-exporter")
    dcgm_exporter_flux_kustomizations.dcgm_exporter(
        flux_chart, dcgm_exporter_artifact, nvidia_device_plugin_kustomization, monitoring_crds_kustomization
    )
    cert_manager_trust_artifact = artifact("cert-manager-trust", "cluster/k8s/cert-manager/trust")
    cert_manager_trust_kustomization = cert_manager_flux_kustomizations.cert_manager_trust(
        flux_chart, cert_manager_trust_artifact, cert_manager_kustomization, kyverno_kustomization
    )
    cnpg_artifact = artifact("cnpg", "cluster/k8s/cnpg")
    cnpg_kustomization = cnpg_flux_kustomizations.cnpg(flux_chart, cnpg_artifact, cert_manager_kustomization)
    external_secrets_operator_artifact = artifact("external-secrets-operator", "cluster/k8s/external-secrets/operator")
    external_secrets_operator_kustomization = external_secrets_flux_kustomizations.external_secrets_operator(
        flux_chart, external_secrets_operator_artifact, external_secrets_crds_kustomization, cert_manager_kustomization
    )
    external_secrets_config_artifact = artifact("external-secrets-config", "cluster/k8s/external-secrets/config")
    external_secrets_config_kustomization = external_secrets_flux_kustomizations.external_secrets_config(
        flux_chart, external_secrets_config_artifact, external_secrets_operator_kustomization
    )
    gateway_artifact = artifact("gateway", "cluster/k8s/gateway")
    gateway_kustomization = gateway_flux_kustomizations.gateway(
        flux_chart,
        gateway_artifact,
        cert_manager_kustomization,
        kyverno_kustomization,
        cert_manager_issuer_config_kustomization,
    )
    tofu_controller_artifact = artifact("tofu-controller", "cluster/k8s/tofu-controller")
    tofu_controller_kustomization = tofu_controller_flux_kustomizations.tofu_controller(
        flux_chart, tofu_controller_artifact, cert_manager_kustomization, kyverno_kustomization
    )
    volsync_artifact = artifact("volsync", "cluster/k8s/volsync")
    volsync_kustomization = volsync_flux_kustomizations.volsync(
        flux_chart, volsync_artifact, snapshot_controller_kustomization
    )
    agent_shared_rbac_artifact = artifact("agent-shared-rbac", "cluster/k8s/agents/shared-rbac")
    agents_flux_kustomizations.agent_shared_rbac(
        flux_chart, agent_shared_rbac_artifact, claude_rbac_kustomization, kyverno_policies_kustomization
    )
    agent_shared_secrets_artifact = artifact("agent-shared-secrets", "cluster/k8s/agents/shared-secrets")
    agent_shared_secrets_kustomization = agents_flux_kustomizations.agent_shared_secrets(
        flux_chart, agent_shared_secrets_artifact, claude_rbac_kustomization
    )
    external_creds_artifact = artifact("external-creds", external_creds.OUTPUT_DIR)
    external_creds_kustomization = external_creds.external_creds(
        flux_chart, external_creds_artifact, root, claude_rbac_kustomization
    )
    goldilocks_artifact = artifact("goldilocks", goldilocks.OUTPUT_DIR)
    goldilocks_kustomization = goldilocks.goldilocks(flux_chart, goldilocks_artifact, vpa_kustomization)
    clickhouse_schema_artifact = artifact("clickhouse-schema", clickhouse_schema.OUTPUT_DIR)
    clickhouse_schema_kustomization = clickhouse_schema.clickhouse_schema(
        flux_chart, clickhouse_schema_artifact, root, clickhouse_kustomization
    )
    cert_manager_environment_artifact = artifact(
        "cert-manager-environment",
        "cluster/k8s/cert-manager/environment",
        "cluster/k8s/cert-manager/config",
        "cluster/k8s/cert-manager/cluster-ca",
    )
    cert_manager_environment_kustomization = cert_manager_flux_kustomizations.cert_manager_environment(
        flux_chart,
        cert_manager_environment_artifact,
        cert_manager_kustomization,
        cert_manager_trust_kustomization,
        cert_manager_issuer_config_kustomization,
        external_creds_kustomization,
        external_secrets_config_kustomization,
    )
    seaweedfs_filer_db_artifact = artifact("seaweedfs-filer-db", "cluster/k8s/seaweedfs/db")
    seaweedfs_filer_db_kustomization = seaweedfs_flux_kustomizations.seaweedfs_filer_db(
        flux_chart, seaweedfs_filer_db_artifact, seaweedfs_namespace_kustomization, cnpg_kustomization
    )
    tofu_state_db_artifact = artifact("tofu-state-db", tofu_state_db.OUTPUT_DIR)
    tofu_state_db_kustomization = tofu_state_db.tofu_state_db(flux_chart, tofu_state_db_artifact, cnpg_kustomization)
    seaweedfs_secrets_artifact = artifact("seaweedfs-secrets", "cluster/k8s/seaweedfs/secrets")
    seaweedfs_secrets_kustomization = seaweedfs_flux_kustomizations.seaweedfs_secrets(
        flux_chart,
        seaweedfs_secrets_artifact,
        seaweedfs_namespace_kustomization,
        external_secrets_operator_kustomization,
    )
    proxmox_proxy_artifact = artifact("proxmox-proxy", "cluster/k8s/proxmox-proxy")
    proxmox_proxy_flux_kustomizations.proxmox_proxy(flux_chart, proxmox_proxy_artifact, gateway_kustomization)
    website_artifact = artifact("website", "cluster/k8s/website")
    website_flux_kustomizations.website(flux_chart, website_artifact, gateway_kustomization)
    kube_system_artifact = artifact("kube-system", kube_system.OUTPUT_DIR)
    kube_system.kube_system(flux_chart, kube_system_artifact, goldilocks_kustomization)
    agents_flux_kustomizations.agents_mitmproxy(flux_chart, cert_manager_trust_kustomization)
    docker_ci_artifact = artifact("docker-ci", "cluster/k8s/parked/docker-ci")
    parked_flux_kustomizations.docker_ci(
        flux_chart, docker_ci_artifact, cert_manager_environment_kustomization, claude_rbac_kustomization
    )
    atuin_artifact = artifact("atuin", "cluster/k8s/atuin")
    atuin_kustomization = atuin_flux_kustomizations.atuin(
        flux_chart, atuin_artifact, cert_manager_issuer_config_kustomization, cnpg_kustomization
    )
    authentik_artifact = artifact("authentik", "cluster/k8s/authentik")
    authentik_kustomization = authentik_flux_kustomizations.authentik(
        flux_chart, authentik_artifact, cnpg_kustomization, monitoring_crds_kustomization
    )
    dns_automation_artifact = artifact("dns-automation", "cluster/k8s/dns-automation")
    dns_automation_flux_kustomizations.dns_automation(
        flux_chart,
        dns_automation_artifact,
        tofu_controller_kustomization,
        tofu_state_db_kustomization,
        external_creds_kustomization,
        external_secrets_config_kustomization,
    )
    forgejo_agentydragon_artifact = artifact("forgejo-agentydragon", forgejo_gitops_modules.AGENTYDRAGON_DIR)
    forgejo_gitops_modules.forgejo_agentydragon(
        flux_chart, forgejo_agentydragon_artifact, tofu_controller_kustomization, tofu_state_db_kustomization
    )
    infra_drift_artifact = artifact("infra-drift", drift_watch.OUTPUT_DIR)
    infra_drift_flux_kustomizations.infra_drift(
        flux_chart, infra_drift_artifact, tofu_controller_kustomization, tofu_state_db_kustomization
    )
    alloy_otlp_bearer_artifact = artifact("alloy-otlp-bearer", "cluster/k8s/agents/alloy-otlp-bearer")
    agents_flux_kustomizations.alloy_otlp_bearer(
        flux_chart,
        alloy_otlp_bearer_artifact,
        external_secrets_config_kustomization,
        claude_rbac_kustomization,
        haku_rbac_kustomization,
    )
    github_secrets_sync_secrets_artifact = artifact(
        "github-secrets-sync-secrets", "cluster/k8s/github-secrets-sync/secrets"
    )
    github_secrets_sync_secrets_kustomization = github_secrets_sync_flux_kustomizations.github_secrets_sync_secrets(
        flux_chart,
        github_secrets_sync_secrets_artifact,
        external_creds_kustomization,
        external_secrets_config_kustomization,
    )
    ntfy_artifact = artifact("ntfy", ntfy.OUTPUT_DIR)
    ntfy_kustomization = ntfy.ntfy(
        flux_chart,
        ntfy_artifact,
        root,
        cnpg_kustomization,
        external_secrets_config_kustomization,
        gateway_kustomization,
        monitoring_crds_kustomization,
    )
    ollama_app_artifact = artifact("ollama-app", "cluster/k8s/ollama")
    ollama_kustomization = ollama_flux_kustomizations.ollama(
        flux_chart,
        ollama_app_artifact,
        gateway_kustomization,
        cert_manager_environment_kustomization,
        nvidia_runtimeclass_kustomization,
        external_secrets_config_kustomization,
        reflector_kustomization,
        claude_rbac_kustomization,
    )
    seaweedfs_cluster_artifact = artifact("seaweedfs-cluster", "cluster/k8s/seaweedfs/cluster")
    seaweedfs_cluster_kustomization = seaweedfs_flux_kustomizations.seaweedfs_cluster(
        flux_chart,
        seaweedfs_cluster_artifact,
        seaweedfs_operator_kustomization,
        seaweedfs_secrets_kustomization,
        seaweedfs_filer_db_kustomization,
        local_path_provisioner_kustomization,
    )
    atuin_user_provisioner_artifact = artifact("atuin-user-provisioner", "cluster/k8s/atuin/user-provisioner")
    atuin_flux_kustomizations.atuin_user_provisioner(
        flux_chart, atuin_user_provisioner_artifact, atuin_kustomization, user_agentydragon_kustomization
    )
    agent_machine_access_tf_artifact = artifact("agent-machine-access-tf", agent_machine_access.OUTPUT_DIR)
    agent_machine_access_tf_kustomization = agent_machine_access.agent_machine_access_tf(
        flux_chart,
        agent_machine_access_tf_artifact,
        tofu_controller_kustomization,
        tofu_state_db_kustomization,
        authentik_kustomization,
    )
    sso_providers_tf_artifact = artifact("sso-providers-tf", sso_providers.OUTPUT_DIR)
    sso_providers_tf_kustomization = sso_providers.sso_providers_tf(
        flux_chart,
        sso_providers_tf_artifact,
        tofu_controller_kustomization,
        tofu_state_db_kustomization,
        authentik_kustomization,
    )
    gatus_sso_tf_artifact = artifact("gatus-sso-tf", gatus_sso.OUTPUT_DIR)
    gatus_sso.gatus_sso_tf(
        flux_chart,
        gatus_sso_tf_artifact,
        tofu_controller_kustomization,
        tofu_state_db_kustomization,
        authentik_kustomization,
    )
    flux_webhook_token_artifact = artifact("flux-webhook-token", flux_webhook_token.OUTPUT_DIR)
    flux_webhook_token_kustomization = flux_webhook_token.flux_webhook_token(
        flux_chart,
        flux_webhook_token_artifact,
        tofu_controller_kustomization,
        tofu_state_db_kustomization,
        github_secrets_sync_secrets_kustomization,
    )
    github_branch_protection_artifact = artifact("github-branch-protection", github_branch_protection.OUTPUT_DIR)
    github_branch_protection.github_branch_protection(
        flux_chart,
        github_branch_protection_artifact,
        tofu_controller_kustomization,
        tofu_state_db_kustomization,
        github_secrets_sync_secrets_kustomization,
    )
    monitoring_stack_artifact = artifact("monitoring-stack", "cluster/k8s/monitoring/stack")
    monitoring_flux_kustomizations.monitoring_stack(
        flux_chart,
        monitoring_stack_artifact,
        monitoring_namespace_kustomization,
        monitoring_crds_kustomization,
        ntfy_kustomization,
        external_secrets_config_kustomization,
    )
    claude_sandbox_secrets_artifact = artifact("claude-sandbox-secrets", "cluster/k8s/agents/claude-sandbox-secrets")
    agents_flux_kustomizations.claude_sandbox_secrets(
        flux_chart, claude_sandbox_secrets_artifact, claude_rbac_kustomization, external_secrets_operator_kustomization
    )
    haku_openclaw_spike_backup_artifact = artifact(
        "haku-openclaw-spike-backup", "cluster/k8s/agents/haku-openclaw-spike/backup"
    )
    agents_flux_kustomizations.haku_openclaw_spike_backup(
        flux_chart, haku_openclaw_spike_backup_artifact, external_secrets_operator_kustomization, volsync_kustomization
    )
    authentik_db_backups_artifact = artifact("authentik-db-backups", "cluster/k8s/authentik/db-backups")
    authentik_flux_kustomizations.authentik_db_backups(
        flux_chart, authentik_db_backups_artifact, cnpg_kustomization, seaweedfs_cluster_kustomization
    )
    monitoring_loki_artifact = artifact("monitoring-loki", "cluster/k8s/monitoring/loki")
    loki_kustomization = monitoring_flux_kustomizations.loki(
        flux_chart, monitoring_loki_artifact, grafana_helmrepository_kustomization, seaweedfs_cluster_kustomization
    )
    monitoring_mimir_artifact = artifact("monitoring-mimir", "cluster/k8s/monitoring/mimir")
    mimir_kustomization = monitoring_flux_kustomizations.mimir(
        flux_chart,
        monitoring_mimir_artifact,
        monitoring_crds_kustomization,
        grafana_helmrepository_kustomization,
        seaweedfs_cluster_kustomization,
    )
    monitoring_tempo_artifact = artifact("monitoring-tempo", "cluster/k8s/monitoring/tempo")
    monitoring_flux_kustomizations.tempo(
        flux_chart,
        monitoring_tempo_artifact,
        monitoring_crds_kustomization,
        grafana_helmrepository_kustomization,
        seaweedfs_cluster_kustomization,
    )
    seaweedfs_drivefs_artifacts_bucket_artifact = artifact(
        "seaweedfs-drivefs-artifacts-bucket", "cluster/k8s/seaweedfs/drivefs-artifacts-bucket"
    )
    seaweedfs_drivefs_artifacts_bucket_kustomization = seaweedfs_flux_kustomizations.seaweedfs_drivefs_artifacts_bucket(
        flux_chart, seaweedfs_drivefs_artifacts_bucket_artifact, seaweedfs_cluster_kustomization
    )
    seaweedfs_external_credentials_artifact = artifact(
        "seaweedfs-external-credentials", "cluster/k8s/seaweedfs/external-credentials"
    )
    seaweedfs_external_credentials_kustomization = seaweedfs_flux_kustomizations.seaweedfs_external_credentials(
        flux_chart,
        seaweedfs_external_credentials_artifact,
        seaweedfs_secrets_kustomization,
        seaweedfs_cluster_kustomization,
    )
    seaweedfs_forgejo_bucket_artifact = artifact("seaweedfs-forgejo-bucket", "cluster/k8s/seaweedfs/forgejo-bucket")
    seaweedfs_flux_kustomizations.seaweedfs_forgejo_bucket(
        flux_chart, seaweedfs_forgejo_bucket_artifact, seaweedfs_cluster_kustomization
    )
    seaweedfs_loom_gym_bucket_artifact = artifact("seaweedfs-loom-gym-bucket", "cluster/k8s/seaweedfs/loom-gym-bucket")
    seaweedfs_flux_kustomizations.seaweedfs_loom_gym_bucket(
        flux_chart, seaweedfs_loom_gym_bucket_artifact, seaweedfs_cluster_kustomization
    )
    seaweedfs_monitoring_artifact = artifact("seaweedfs-monitoring", "cluster/k8s/seaweedfs/monitoring")
    seaweedfs_flux_kustomizations.seaweedfs_monitoring(
        flux_chart, seaweedfs_monitoring_artifact, seaweedfs_cluster_kustomization, monitoring_crds_kustomization
    )
    seaweedfs_pr_visuals_bucket_artifact = artifact(
        "seaweedfs-pr-visuals-bucket", "cluster/k8s/seaweedfs/pr-visuals-bucket"
    )
    seaweedfs_pr_visuals_bucket_kustomization = seaweedfs_flux_kustomizations.seaweedfs_pr_visuals_bucket(
        flux_chart, seaweedfs_pr_visuals_bucket_artifact, seaweedfs_cluster_kustomization
    )
    seaweedfs_public_coder_agent_backups_bucket_artifact = artifact(
        "seaweedfs-public-coder-agent-backups-bucket", "cluster/k8s/seaweedfs/public-coder-agent-backups-bucket"
    )
    seaweedfs_public_coder_agent_backups_bucket_kustomization = (
        seaweedfs_flux_kustomizations.seaweedfs_public_coder_agent_backups_bucket(
            flux_chart, seaweedfs_public_coder_agent_backups_bucket_artifact, seaweedfs_cluster_kustomization
        )
    )
    seaweedfs_registry_cache_bucket_artifact = artifact(
        "seaweedfs-registry-cache-bucket", "cluster/k8s/seaweedfs/registry-cache-bucket"
    )
    seaweedfs_registry_cache_bucket_kustomization = seaweedfs_flux_kustomizations.seaweedfs_registry_cache_bucket(
        flux_chart, seaweedfs_registry_cache_bucket_artifact, seaweedfs_cluster_kustomization
    )
    seaweedfs_csi_artifact = artifact("seaweedfs-csi", "cluster/k8s/seaweedfs-csi")
    seaweedfs_csi_flux_kustomizations.seaweedfs_csi(flux_chart, seaweedfs_csi_artifact, seaweedfs_cluster_kustomization)
    vm_images_publisher_artifact = artifact("vm-images-publisher", "cluster/k8s/vm-images-publisher")
    vm_images_publisher_kustomization = vm_images_publisher_flux_kustomizations.vm_images_publisher(
        flux_chart, vm_images_publisher_artifact, seaweedfs_cluster_kustomization
    )
    kubectl_passthrough_mcp_artifact = artifact(
        "kubectl-passthrough-mcp", "cluster/k8s/agents/kubectl-passthrough-mcp/app"
    )
    agents_flux_kustomizations.kubectl_passthrough_mcp(flux_chart, kubectl_passthrough_mcp_artifact)
    forgejo_artifact = artifact("forgejo", "cluster/k8s/forgejo")
    forgejo_kustomization = forgejo_flux_kustomizations.forgejo(
        flux_chart,
        forgejo_artifact,
        cnpg_kustomization,
        external_secrets_operator_kustomization,
        seaweedfs_operator_kustomization,
        monitoring_crds_kustomization,
    )
    headlamp_app_artifact = artifact("headlamp-app", "cluster/k8s/headlamp")
    headlamp_flux_kustomizations.headlamp(
        flux_chart, headlamp_app_artifact, gateway_kustomization, sso_providers_tf_kustomization
    )
    matrix_app_artifact = artifact("matrix-app", "cluster/k8s/matrix")
    matrix_kustomization = matrix_flux_kustomizations.matrix(flux_chart, matrix_app_artifact, cnpg_kustomization)
    grafana_instance_artifact = artifact("grafana-instance", "cluster/k8s/monitoring/grafana-instance")
    grafana_instance_kustomization = monitoring_flux_kustomizations.grafana_instance(
        flux_chart, grafana_instance_artifact, grafana_operator_kustomization, cnpg_kustomization
    )
    gatus_artifact = artifact("gatus", "cluster/k8s/gatus")
    gatus_flux_kustomizations.gatus(flux_chart, gatus_artifact, cnpg_kustomization, monitoring_crds_kustomization)
    flux_webhook_artifact = artifact("flux-webhook", "cluster/k8s/flux-webhook")
    flux_webhook_flux_kustomizations.flux_webhook(
        flux_chart,
        flux_webhook_artifact,
        flux_webhook_token_kustomization,
        ntfy_kustomization,
        external_secrets_config_kustomization,
        gateway_kustomization,
    )
    langfuse_artifact = artifact("langfuse", "cluster/k8s/langfuse")
    langfuse_flux_kustomizations.langfuse(
        flux_chart, langfuse_artifact, cnpg_kustomization, valkey_kustomization, seaweedfs_operator_kustomization
    )
    vector_talos_logs_artifact = artifact("vector-talos-logs", "cluster/k8s/vector-talos-logs")
    vector_talos_logs_flux_kustomizations.vector_talos_logs(flux_chart, vector_talos_logs_artifact, loki_kustomization)
    monitoring_alloy_artifact = artifact("monitoring-alloy", "cluster/k8s/monitoring/alloy")
    monitoring_flux_kustomizations.alloy(
        flux_chart, monitoring_alloy_artifact, mimir_kustomization, grafana_helmrepository_kustomization
    )
    public_coder_agent_backup_artifact = artifact(
        "public-coder-agent-backup", "cluster/k8s/agents/public-coder-agent/backup"
    )
    agents_flux_kustomizations.public_coder_agent_backup(
        flux_chart,
        public_coder_agent_backup_artifact,
        seaweedfs_public_coder_agent_backups_bucket_kustomization,
        external_secrets_config_kustomization,
        volsync_kustomization,
    )
    oci_cache_artifact = artifact("oci-cache", "cluster/k8s/oci-cache")
    oci_cache_flux_kustomizations.oci_cache(
        flux_chart,
        oci_cache_artifact,
        valkey_kustomization,
        seaweedfs_registry_cache_bucket_kustomization,
        monitoring_crds_kustomization,
    )
    seaweedfs_public_s3_artifact = artifact("seaweedfs-public-s3", "cluster/k8s/seaweedfs/public-s3")
    seaweedfs_public_s3_kustomization = seaweedfs_flux_kustomizations.seaweedfs_public_s3(
        flux_chart,
        seaweedfs_public_s3_artifact,
        seaweedfs_external_credentials_kustomization,
        seaweedfs_drivefs_artifacts_bucket_kustomization,
        vm_images_publisher_kustomization,
        seaweedfs_secrets_kustomization,
        seaweedfs_cluster_kustomization,
        gateway_kustomization,
    )
    haku_cloud_agent_artifact = artifact("haku-cloud-agent", "cluster/k8s/parked/cloud-agent-tf")
    parked_flux_kustomizations.haku_cloud_agent(
        flux_chart,
        haku_cloud_agent_artifact,
        external_creds_kustomization,
        external_secrets_config_kustomization,
        tofu_controller_kustomization,
        tofu_state_db_kustomization,
    )
    forgejo_agentydragon_repos_artifact = artifact(
        "forgejo-agentydragon-repos", forgejo_gitops_modules.AGENTYDRAGON_REPOS_DIR
    )
    forgejo_agentydragon_repos_kustomization = forgejo_gitops_modules.forgejo_agentydragon_repos(
        flux_chart,
        forgejo_agentydragon_repos_artifact,
        forgejo_kustomization,
        tofu_controller_kustomization,
        tofu_state_db_kustomization,
    )
    budget_ledger_artifact = artifact("budget-ledger", forgejo_gitops_modules.BUDGET_LEDGER_DIR)
    forgejo_gitops_modules.budget_ledger(
        flux_chart,
        budget_ledger_artifact,
        forgejo_kustomization,
        tofu_controller_kustomization,
        tofu_state_db_kustomization,
        budget_namespace_kustomization,
    )
    forgejo_claude_artifact = artifact("forgejo-claude", forgejo_gitops_modules.CLAUDE_DIR)
    forgejo_claude_kustomization = forgejo_gitops_modules.forgejo_claude(
        flux_chart,
        forgejo_claude_artifact,
        forgejo_kustomization,
        tofu_controller_kustomization,
        tofu_state_db_kustomization,
        claude_rbac_kustomization,
    )
    forgejo_images_artifact = artifact("forgejo-images", forgejo_images.OUTPUT_DIR)
    forgejo_images_kustomization = forgejo_images_flux_kustomizations.forgejo_images(
        flux_chart,
        forgejo_images_artifact,
        external_secrets_config_kustomization,
        forgejo_kustomization,
        tofu_controller_kustomization,
        tofu_state_db_kustomization,
    )
    haku_ci_artifact = artifact("haku-ci", "cluster/k8s/haku-ci")
    haku_ci_flux_kustomizations.haku_ci(flux_chart, haku_ci_artifact, keda_kustomization)
    flux_grafana_secrets_artifact = artifact("flux-grafana-secrets", "cluster/k8s/flux-grafana-secrets")
    flux_grafana_secrets_flux_kustomizations.flux_grafana_secrets(
        flux_chart, flux_grafana_secrets_artifact, grafana_instance_kustomization, grafana_operator_kustomization
    )
    clickhouse_grafana_artifact = artifact("clickhouse-grafana", "cluster/k8s/grafana")
    grafana_flux_kustomizations.clickhouse_grafana(
        flux_chart, clickhouse_grafana_artifact, clickhouse_kustomization, grafana_instance_kustomization
    )
    agent_box_artifact = artifact("agent-box", "cluster/k8s/parked/agent-box")
    parked_flux_kustomizations.agent_box(
        flux_chart,
        agent_box_artifact,
        kubevirt_kustomization,
        cdi_kustomization,
        external_secrets_operator_kustomization,
        seaweedfs_public_s3_kustomization,
        local_path_provisioner_kustomization,
    )
    gecko_artifact = artifact("gecko", "cluster/k8s/parked/gecko/app")
    parked_flux_kustomizations.gecko(
        flux_chart,
        gecko_artifact,
        gecko_namespace_kustomization,
        kubevirt_kustomization,
        cdi_kustomization,
        external_secrets_operator_kustomization,
        seaweedfs_public_s3_kustomization,
        local_path_provisioner_kustomization,
    )
    activitywatch_artifact = artifact("activitywatch", "cluster/k8s/activitywatch")
    activitywatch_flux_kustomizations.activitywatch(
        flux_chart,
        activitywatch_artifact,
        external_secrets_config_kustomization,
        forgejo_images_kustomization,
        local_path_provisioner_kustomization,
    )
    agentplane_index_artifact = artifact("agentplane-index", "cluster/k8s/agentplane-index")
    agentplane_index_kustomization = agentplane_index_flux_kustomizations.agentplane_index(
        flux_chart,
        agentplane_index_artifact,
        cnpg_kustomization,
        external_secrets_config_kustomization,
        forgejo_images_kustomization,
        local_path_provisioner_kustomization,
        ollama_kustomization,
    )
    airlock_artifact = artifact("airlock", "cluster/k8s/agents/airlock")
    agents_flux_kustomizations.airlock(flux_chart, airlock_artifact, external_secrets_operator_kustomization)
    authentik_jwt_rotation_artifact = artifact("authentik-jwt-rotation", "cluster/k8s/agents/authentik-jwt-rotation")
    authentik_jwt_rotation_kustomization = agents_flux_kustomizations.authentik_jwt_rotation(
        flux_chart, authentik_jwt_rotation_artifact, external_secrets_operator_kustomization
    )
    loki_read_proxy_artifact = artifact("loki-read-proxy", "cluster/k8s/agents/loki-read-proxy")
    agents_flux_kustomizations.loki_read_proxy(
        flux_chart, loki_read_proxy_artifact, external_secrets_operator_kustomization
    )
    plaid_mcp_artifact = artifact("plaid-mcp", "cluster/k8s/agents/plaid-mcp")
    agents_flux_kustomizations.plaid_mcp(
        flux_chart,
        plaid_mcp_artifact,
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
    tana_mcp_artifact = artifact("tana-mcp", "cluster/k8s/agents/tana-mcp")
    agents_flux_kustomizations.tana_mcp(
        flux_chart,
        tana_mcp_artifact,
        external_creds_kustomization,
        external_secrets_config_kustomization,
        valkey_kustomization,
        monitoring_crds_kustomization,
    )
    cli_proxy_api_artifact = artifact("cli-proxy-api", "cluster/k8s/cli-proxy-api")
    cli_proxy_api_kustomization = cli_proxy_api_flux_kustomizations.cli_proxy_api(
        flux_chart,
        cli_proxy_api_artifact,
        external_secrets_config_kustomization,
        gateway_kustomization,
        cert_manager_environment_kustomization,
        sso_providers_tf_kustomization,
        forgejo_images_kustomization,
    )
    cpap_sync_artifact = artifact("cpap-sync", "cluster/k8s/cpap-sync")
    cpap_sync_kustomization = cpap_sync_flux_kustomizations.cpap_sync(
        flux_chart,
        cpap_sync_artifact,
        external_secrets_config_kustomization,
        kubevirt_kustomization,
        forgejo_images_kustomization,
    )
    flux_image_automation_forgejo_artifact = artifact(
        "flux-image-automation-forgejo", forgejo_image_automation.OUTPUT_DIR
    )
    forgejo_image_automation.flux_image_automation_forgejo(
        flux_chart,
        flux_image_automation_forgejo_artifact,
        forgejo_images_kustomization,
        flux_image_automation_ghcr_kustomization,
    )
    github_api_proxy_artifact = artifact("github-api-proxy", "cluster/k8s/github-api-proxy")
    github_api_proxy_flux_kustomizations.github_api_proxy(
        flux_chart,
        github_api_proxy_artifact,
        external_secrets_operator_kustomization,
        cert_manager_kustomization,
        cert_manager_issuer_config_kustomization,
        monitoring_crds_kustomization,
    )
    github_exporter_artifact = artifact("github-exporter", "cluster/k8s/github-exporter")
    github_exporter_flux_kustomizations.github_exporter(
        flux_chart,
        github_exporter_artifact,
        forgejo_images_kustomization,
        monitoring_namespace_kustomization,
        monitoring_crds_kustomization,
        grafana_instance_kustomization,
        external_secrets_config_kustomization,
        external_creds_kustomization,
    )
    github_secrets_sync_artifact = artifact("github-secrets-sync", github_secrets_sync_gitops_module.OUTPUT_DIR)
    github_secrets_sync_gitops_module.github_secrets_sync(
        flux_chart,
        github_secrets_sync_artifact,
        tofu_controller_kustomization,
        tofu_state_db_kustomization,
        github_secrets_sync_secrets_kustomization,
        forgejo_images_kustomization,
        seaweedfs_pr_visuals_bucket_kustomization,
    )
    grocy_sf_artifact = artifact("grocy-sf", "cluster/k8s/grocy/sf/app", "cluster/k8s/grocy/app-base")
    grocy_sf_kustomization = grocy_flux_kustomizations.grocy_sf(
        flux_chart,
        grocy_sf_artifact,
        forgejo_images_kustomization,
        gateway_kustomization,
        cert_manager_issuer_config_kustomization,
        cert_manager_environment_kustomization,
        authentik_kustomization,
        volsync_kustomization,
    )
    grocy_vallejo_artifact = artifact("grocy-vallejo", "cluster/k8s/grocy/vallejo/app", "cluster/k8s/grocy/app-base")
    grocy_vallejo_kustomization = grocy_flux_kustomizations.grocy_vallejo(
        flux_chart,
        grocy_vallejo_artifact,
        forgejo_images_kustomization,
        gateway_kustomization,
        cert_manager_issuer_config_kustomization,
        cert_manager_environment_kustomization,
        authentik_kustomization,
        volsync_kustomization,
    )
    haku_mailbox_artifact = artifact("haku-mailbox", "cluster/k8s/haku/mailbox")
    haku_flux_kustomizations.haku_mailbox(
        flux_chart,
        haku_mailbox_artifact,
        cnpg_kustomization,
        cert_manager_kustomization,
        external_secrets_operator_kustomization,
        cert_manager_issuer_config_kustomization,
    )
    home_assistant_artifact = artifact("home-assistant", "cluster/k8s/home-assistant")
    home_assistant_kustomization = home_assistant_flux_kustomizations.home_assistant(
        flux_chart,
        home_assistant_artifact,
        local_path_provisioner_kustomization,
        seaweedfs_cluster_kustomization,
        volsync_kustomization,
        external_secrets_config_kustomization,
        forgejo_images_kustomization,
        monitoring_crds_kustomization,
        gateway_kustomization,
        sso_providers_tf_kustomization,
    )
    matrix_user_provisioner_artifact = artifact("matrix-user-provisioner", "cluster/k8s/matrix/user-provisioner")
    matrix_flux_kustomizations.matrix_user_provisioner(
        flux_chart,
        matrix_user_provisioner_artifact,
        external_secrets_config_kustomization,
        forgejo_images_kustomization,
        matrix_kustomization,
    )
    nix_cache_artifact = artifact("nix-cache", "cluster/k8s/nix-cache")
    nix_cache_flux_kustomizations.nix_cache(
        flux_chart,
        nix_cache_artifact,
        cnpg_kustomization,
        external_creds_kustomization,
        external_secrets_config_kustomization,
        forgejo_images_kustomization,
        gateway_kustomization,
        cert_manager_kustomization,
        seaweedfs_cluster_kustomization,
    )
    sdr_artifact = artifact("sdr", "cluster/k8s/parked/sdr")
    parked_flux_kustomizations.sdr(
        flux_chart,
        sdr_artifact,
        external_secrets_config_kustomization,
        forgejo_images_kustomization,
        gateway_kustomization,
        authentik_kustomization,
    )
    ssh_mcp_artifact = artifact("ssh-mcp", ssh_mcp_generation.OUTPUT_DIR)
    ssh_mcp_kustomization = ssh_mcp_generation.ssh_mcp(
        flux_chart, ssh_mcp_artifact, root, mesh, devbox_service, external_secrets_operator_kustomization
    )
    google_mcp_artifact = artifact("google-mcp", google_mcp.OUTPUT_DIR)
    google_mcp.google_mcp(
        flux_chart, google_mcp_artifact, root, external_secrets_operator_kustomization, forgejo_images_kustomization
    )
    study_casino_artifact = artifact("study-casino", "cluster/k8s/study-casino")
    study_casino_flux_kustomizations.study_casino(
        flux_chart, study_casino_artifact, cnpg_kustomization, external_secrets_operator_kustomization
    )
    haku_state_artifact = artifact("haku-state", forgejo_gitops_modules.HAKU_STATE_DIR)
    haku_state_kustomization = forgejo_gitops_modules.haku_state(
        flux_chart,
        haku_state_artifact,
        forgejo_kustomization,
        tofu_controller_kustomization,
        tofu_state_db_kustomization,
        agentplane_index_kustomization,
        haku_namespace_kustomization,
    )
    monitoring_alloy_otlp_bearer_token_tf_artifact = artifact(
        "monitoring-alloy-otlp-bearer-token-tf", alloy_otlp_bearer_token.OUTPUT_DIR
    )
    alloy_otlp_bearer_token.alloy_otlp_bearer_token_tf(
        flux_chart,
        monitoring_alloy_otlp_bearer_token_tf_artifact,
        tofu_controller_kustomization,
        tofu_state_db_kustomization,
        authentik_jwt_rotation_kustomization,
    )
    litellm_artifact = artifact("litellm", "cluster/k8s/litellm")
    litellm_kustomization = litellm_proxy.litellm(
        flux_chart,
        litellm_artifact,
        root,
        cnpg_kustomization,
        external_secrets_operator_kustomization,
        monitoring_crds_kustomization,
    )
    aiquota_artifact = artifact("aiquota", aiquota.OUTPUT_DIR)
    aiquota.aiquota(
        flux_chart,
        aiquota_artifact,
        root,
        external_secrets_config_kustomization,
        forgejo_images_kustomization,
        cli_proxy_api_kustomization,
        external_secrets_operator_kustomization,
        clickhouse_schema_kustomization,
        agent_machine_access_tf_kustomization,
        reflector_kustomization,
    )
    cpap_data_artifact = artifact("cpap-data", forgejo_gitops_modules.CPAP_DATA_DIR)
    forgejo_gitops_modules.cpap_data(
        flux_chart,
        cpap_data_artifact,
        forgejo_kustomization,
        tofu_controller_kustomization,
        tofu_state_db_kustomization,
        cpap_sync_kustomization,
    )
    grocy_mcp_sf_artifact = artifact(
        "grocy-mcp-sf",
        "cluster/k8s/grocy/sf/mcp",
        "cluster/k8s/grocy/mcp-base",
        "cluster/k8s/grocy/mcp-servicemonitor-base",
    )
    grocy_flux_kustomizations.grocy_mcp_sf(
        flux_chart,
        grocy_mcp_sf_artifact,
        external_secrets_config_kustomization,
        forgejo_images_kustomization,
        gateway_kustomization,
        grocy_sf_kustomization,
        valkey_kustomization,
        agent_machine_access_tf_kustomization,
        reflector_kustomization,
        monitoring_crds_kustomization,
    )
    grocy_sf_user_perms_artifact = artifact(
        "grocy-sf-user-perms", "cluster/k8s/grocy/sf/user-perms", "cluster/k8s/grocy/user-perms-base"
    )
    grocy_flux_kustomizations.grocy_sf_user_perms(
        flux_chart, grocy_sf_user_perms_artifact, forgejo_images_kustomization, grocy_sf_kustomization
    )
    grocy_mcp_vallejo_artifact = artifact(
        "grocy-mcp-vallejo",
        "cluster/k8s/grocy/vallejo/mcp",
        "cluster/k8s/grocy/mcp-base",
        "cluster/k8s/grocy/mcp-servicemonitor-base",
    )
    grocy_flux_kustomizations.grocy_mcp_vallejo(
        flux_chart,
        grocy_mcp_vallejo_artifact,
        external_secrets_config_kustomization,
        forgejo_images_kustomization,
        gateway_kustomization,
        grocy_vallejo_kustomization,
        valkey_kustomization,
        agent_machine_access_tf_kustomization,
        reflector_kustomization,
        monitoring_crds_kustomization,
    )
    grocy_vallejo_user_perms_artifact = artifact(
        "grocy-vallejo-user-perms", "cluster/k8s/grocy/vallejo/user-perms", "cluster/k8s/grocy/user-perms-base"
    )
    grocy_flux_kustomizations.grocy_vallejo_user_perms(
        flux_chart, grocy_vallejo_user_perms_artifact, forgejo_images_kustomization, grocy_vallejo_kustomization
    )
    ha_mcp_artifact = artifact("ha-mcp", ha_mcp.OUTPUT_DIR)
    ha_mcp.ha_mcp(
        flux_chart,
        ha_mcp_artifact,
        root,
        external_secrets_config_kustomization,
        forgejo_images_kustomization,
        home_assistant_kustomization,
        monitoring_crds_kustomization,
    )
    forgejo_token_rotation_artifact = artifact("forgejo-token-rotation", "cluster/k8s/agents/forgejo-token-rotation")
    agents_flux_kustomizations.forgejo_token_rotation(
        flux_chart,
        forgejo_token_rotation_artifact,
        forgejo_images_kustomization,
        authentik_jwt_rotation_kustomization,
        forgejo_claude_kustomization,
        haku_state_kustomization,
        forgejo_agentydragon_repos_kustomization,
    )
    haku_egress_proxy_artifact = artifact("haku-egress-proxy", "cluster/k8s/agents/haku-egress-proxy")
    haku_egress_proxy_kustomization = agents_flux_kustomizations.haku_egress_proxy(
        flux_chart,
        haku_egress_proxy_artifact,
        cert_manager_kustomization,
        cert_manager_trust_kustomization,
        external_secrets_operator_kustomization,
    )
    haku_ui_image_webhook_artifact = artifact("haku-ui-image-webhook", "cluster/k8s/haku/ui-image-webhook")
    haku_flux_kustomizations.haku_ui_image_webhook(flux_chart, haku_ui_image_webhook_artifact, haku_state_kustomization)
    haku_workloads_artifact = artifact("haku-workloads", "cluster/k8s/haku/workloads")
    haku_flux_kustomizations.haku_workloads(flux_chart, haku_workloads_artifact, haku_state_kustomization)
    litellm_keys_tf_artifact = artifact("litellm-keys-tf", litellm_keys.OUTPUT_DIR)
    litellm_keys_tf_kustomization = litellm_keys.litellm_keys_tf(
        flux_chart,
        litellm_keys_tf_artifact,
        litellm_kustomization,
        tofu_controller_kustomization,
        tofu_state_db_kustomization,
    )
    haku_openclaw_spike_app_artifact = artifact("haku-openclaw-spike-app", "cluster/k8s/agents/haku-openclaw-spike/app")
    agents_flux_kustomizations.haku_openclaw_spike_app(
        flux_chart,
        haku_openclaw_spike_app_artifact,
        external_secrets_operator_kustomization,
        seaweedfs_operator_kustomization,
    )
    haku_workspaces_app_artifact = artifact("haku-workspaces-app", "cluster/k8s/haku/workspaces/app")
    haku_flux_kustomizations.haku_workspaces(
        flux_chart,
        haku_workspaces_app_artifact,
        agent_sandbox_controller_kustomization,
        haku_rbac_kustomization,
        haku_egress_proxy_kustomization,
        kyverno_policies_kustomization,
        external_secrets_config_kustomization,
    )
    haku_managed_agent_artifact = artifact("haku-managed-agent", "haku/runtime/managed_agent/self_hosted/deploy")
    parked_flux_kustomizations.haku_managed_agent(
        flux_chart,
        haku_managed_agent_artifact,
        forgejo_images_kustomization,
        external_creds_kustomization,
        external_secrets_config_kustomization,
        haku_namespace_kustomization,
        haku_rbac_kustomization,
        haku_state_kustomization,
        haku_egress_proxy_kustomization,
    )
    agentplane_testing_artifact = artifact("agentplane-testing", f"cluster/k8s/{testing.ENV.namespace}")
    testing.agentplane_testing(
        flux_chart,
        agentplane_testing_artifact,
        agentplane_testing_health_checks,
        agentplane_crds_kustomization,
        agent_sandbox_controller_kustomization,
        cert_manager_environment_kustomization,
        cert_manager_trust_kustomization,
        claude_rbac_kustomization,
        cnpg_kustomization,
        external_secrets_config_kustomization,
    )
    agent_workspaces_app_artifact = artifact("agent-workspaces-app", "cluster/k8s/agents/agent-sandbox/workspaces")
    agents_flux_kustomizations.agent_workspaces_app(
        flux_chart,
        agent_workspaces_app_artifact,
        external_secrets_config_kustomization,
        agent_sandbox_controller_kustomization,
        kyverno_policies_kustomization,
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
    haku_console_artifact = artifact("haku-console", haku_charts.PATH)
    haku_charts.haku_console(
        flux_chart,
        haku_console_artifact,
        haku_console_health_checks,
        cnpg_kustomization,
        local_path_provisioner_kustomization,
        forgejo_images_kustomization,
        gateway_kustomization,
        agent_machine_access_tf_kustomization,
        reflector_kustomization,
        external_creds_kustomization,
        external_secrets_config_kustomization,
        ssh_mcp_kustomization,
        monitoring_crds_kustomization,
    )
    public_coder_agent_app_artifact = artifact(
        "public-coder-agent-app",
        "cluster/k8s/agents/public-coder-agent/app",
        "cluster/k8s/agents/public-coder-agent/namespace",
        "cluster/k8s/agents/public-coder-agent/proxy",
        "cluster/k8s/agents/public-coder-agent/sshpiper",
    )
    public_coder_agent_app_kustomization = agents_flux_kustomizations.public_coder_agent_app(
        flux_chart,
        public_coder_agent_app_artifact,
        cert_manager_kustomization,
        cert_manager_trust_kustomization,
        external_secrets_operator_kustomization,
        sshpiper_crds_kustomization,
    )
    public_coder_agent_devbox_artifact = artifact(
        "public-coder-agent-devbox", "cluster/k8s/agents/public-coder-agent/devbox"
    )
    agents_flux_kustomizations.public_coder_agent_devbox(
        flux_chart,
        public_coder_agent_devbox_artifact,
        kubevirt_kustomization,
        forgejo_images_kustomization,
        external_creds_kustomization,
        external_secrets_config_kustomization,
        agent_shared_secrets_kustomization,
        public_coder_agent_app_kustomization,
    )
    agentplane_staging_artifact = artifact("agentplane-staging", f"cluster/k8s/{staging.ENV.namespace}")
    staging.agentplane_staging(
        flux_chart,
        agentplane_staging_artifact,
        agentplane_staging_health_checks,
        agentplane_crds_kustomization,
        agent_sandbox_controller_kustomization,
        cert_manager_environment_kustomization,
        cert_manager_trust_kustomization,
        claude_rbac_kustomization,
        cnpg_kustomization,
        external_creds_kustomization,
        external_secrets_config_kustomization,
    )
    # Every artifact built above except the parked nodes': those Kustomizations are suspended.
    write_artifact_generators(
        root,
        ducktape=[
            agentplane_staging_artifact,
            monitoring_stack_artifact,
            ntfy_artifact,
            agentplane_testing_artifact,
            claude_rbac_artifact,
            agent_machine_access_tf_artifact,
            authentik_artifact,
            sso_providers_tf_artifact,
            cert_manager_artifact,
            cert_manager_environment_artifact,
            cert_manager_issuer_config_artifact,
            cnpg_artifact,
            external_secrets_config_artifact,
            external_secrets_operator_artifact,
            forgejo_artifact,
            forgejo_images_artifact,
            gateway_artifact,
            kyverno_artifact,
            litellm_keys_tf_artifact,
            local_path_provisioner_artifact,
            reflector_artifact,
            seaweedfs_cluster_artifact,
            seaweedfs_filer_db_artifact,
            seaweedfs_namespace_artifact,
            seaweedfs_operator_artifact,
            seaweedfs_secrets_artifact,
            tofu_controller_artifact,
            tofu_state_db_artifact,
            valkey_artifact,
            kyverno_policies_artifact,
            haku_state_artifact,
            agentplane_crds_artifact,
            monitoring_namespace_artifact,
            haku_namespace_artifact,
            volsync_artifact,
            cert_manager_trust_artifact,
            agent_sandbox_controller_artifact,
            grafana_helmrepository_artifact,
            kubevirt_artifact,
            agentplane_index_artifact,
            clickhouse_artifact,
            github_secrets_sync_secrets_artifact,
            grafana_instance_artifact,
            haku_egress_proxy_artifact,
            haku_rbac_artifact,
            seaweedfs_csi_artifact,
            seaweedfs_public_s3_artifact,
            seaweedfs_external_credentials_artifact,
            tana_mcp_artifact,
            authentik_jwt_rotation_artifact,
            cdi_artifact,
            flux_image_automation_ghcr_artifact,
            grafana_operator_artifact,
            grocy_sf_artifact,
            grocy_vallejo_artifact,
            home_assistant_artifact,
            litellm_artifact,
            nix_cache_artifact,
            nvidia_device_plugin_artifact,
            nvidia_runtimeclass_artifact,
            claude_sandbox_secrets_artifact,
            forgejo_token_rotation_artifact,
            ha_mcp_artifact,
            haku_openclaw_spike_app_artifact,
            haku_openclaw_spike_backup_artifact,
            haku_managed_agent_artifact,
            kubectl_passthrough_mcp_artifact,
            loki_read_proxy_artifact,
            plaid_mcp_artifact,
            public_coder_agent_app_artifact,
            public_coder_agent_backup_artifact,
            activitywatch_artifact,
            agent_workspaces_app_artifact,
            airlock_artifact,
            alloy_otlp_bearer_artifact,
            public_coder_agent_devbox_artifact,
            agent_shared_rbac_artifact,
            agent_shared_secrets_artifact,
            aiquota_artifact,
            clickhouse_grafana_artifact,
            atuin_artifact,
            atuin_user_provisioner_artifact,
            authentik_db_backups_artifact,
            cli_proxy_api_artifact,
            clickhouse_operator_artifact,
            clickhouse_schema_artifact,
            coredns_custom_artifact,
            cpap_sync_artifact,
            dcgm_exporter_artifact,
            descheduler_artifact,
            dns_automation_artifact,
            flux_grafana_secrets_artifact,
            flux_image_automation_forgejo_artifact,
            flux_monitoring_artifact,
            flux_webhook_artifact,
            flux_webhook_token_artifact,
            forgejo_agentydragon_artifact,
            forgejo_agentydragon_repos_artifact,
            budget_ledger_artifact,
            budget_namespace_artifact,
            forgejo_cache_artifact,
            forgejo_claude_artifact,
            cpap_data_artifact,
            gatus_artifact,
            gatus_sso_tf_artifact,
            github_api_proxy_artifact,
            github_branch_protection_artifact,
            github_exporter_artifact,
            github_secrets_sync_artifact,
            goldilocks_artifact,
            google_mcp_artifact,
            grocy_mcp_sf_artifact,
            grocy_sf_user_perms_artifact,
            grocy_mcp_vallejo_artifact,
            grocy_vallejo_user_perms_artifact,
            haku_console_artifact,
            haku_mailbox_artifact,
            haku_forgejo_tea_artifact,
            haku_ui_image_webhook_artifact,
            haku_workloads_artifact,
            haku_workspaces_app_artifact,
            haku_ci_artifact,
            headlamp_app_artifact,
            hubble_ui_artifact,
            infra_drift_artifact,
            keda_artifact,
            kube_api_proxy_artifact,
            kube_system_artifact,
            kubevirt_cdi_operator_artifact,
            kubevirt_operator_artifact,
            langfuse_artifact,
            matrix_app_artifact,
            matrix_user_provisioner_artifact,
            metrics_server_artifact,
            monitoring_alloy_artifact,
            monitoring_alloy_otlp_bearer_token_tf_artifact,
            monitoring_cilium_artifact,
            monitoring_etcd_artifact,
            monitoring_loki_artifact,
            monitoring_mimir_artifact,
            monitoring_rules_artifact,
            monitoring_tempo_artifact,
            node_feature_discovery_artifact,
            oci_cache_artifact,
            ollama_app_artifact,
            openebs_lvm_artifact,
            proxmox_proxy_artifact,
            reloader_artifact,
            seaweedfs_drivefs_artifacts_bucket_artifact,
            seaweedfs_forgejo_bucket_artifact,
            seaweedfs_loom_gym_bucket_artifact,
            seaweedfs_monitoring_artifact,
            seaweedfs_pr_visuals_bucket_artifact,
            seaweedfs_public_coder_agent_backups_bucket_artifact,
            seaweedfs_registry_cache_bucket_artifact,
            ssh_mcp_artifact,
            study_casino_artifact,
            talos_cloud_controller_manager_artifact,
            user_agentydragon_artifact,
            vector_talos_logs_artifact,
            vm_images_publisher_artifact,
            vpa_artifact,
            website_artifact,
        ],
        flux_system=[external_creds_artifact],
    )
    flux_app.synth()


def main() -> None:
    generate_manifests(get_build_workspace_directory())


if __name__ == "__main__":
    main()
