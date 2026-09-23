"""Dispatch manifest generation to each component's local cdk8s helpers."""

from pathlib import Path

from cdk8s import App, Chart

from cluster.cdk8s import (
    agent_machine_access,
    agent_rbac_base,
    agent_shared_rbac,
    agent_workspaces,
    aiquota,
    airlock,
    alloy_otlp_bearer,
    authentik_jwt_rotation,
    claude_sandbox_secrets,
    cnpg_operator,
    descheduler,
    dns_automation,
    ducktape_flux,
    egress_fences,
    etcd,
    external_creds,
    flux_monitoring,
    flux_sources,
    forgejo_image_automation,
    forgejo_images,
    forgejo_token_rotation,
    gateway,
    github_branch_protection,
    goldilocks,
    google_mcp,
    ha_mcp,
    haku_egress_proxy,
    haku_openclaw_spike_backup,
    haku_openclaw_spike_config,
    headlamp,
    hubble_ui,
    keda,
    kube_api_proxy,
    kube_system,
    kubectl_passthrough_mcp,
    local_path_provisioner,
    loki_read_proxy,
    metrics_server,
    mitmproxy,
    node_feature_discovery,
    ntfy,
    nvidia_device_plugin,
    nvidia_runtimeclass,
    proxmox_proxy,
    public_coder_agent_config,
    public_coder_backup,
    public_coder_devbox,
    public_coder_proxy,
    public_coder_sshpiper,
    reflector,
    reloader,
    stateful_infra,
    talos_cloud_controller_manager,
    tana_mcp,
    user_agentydragon,
    valkey,
    vector_talos_logs,
    volsync,
    vpa,
)
from cluster.cdk8s.activitywatch import (
    app as activitywatch_app,
    flux_kustomizations as activitywatch_flux_kustomizations,
)
from cluster.cdk8s.agentplane import generation as agentplane_generation, staging, testing
from cluster.cdk8s.agentplane_crds import flux_kustomizations as agentplane_crds_flux_kustomizations
from cluster.cdk8s.agentplane_index import (
    flux_kustomizations as agentplane_index_flux_kustomizations,
    workers as agentplane_index_workers,
)
from cluster.cdk8s.agents import flux_kustomizations as agents_flux_kustomizations, namespaces as agents_namespaces
from cluster.cdk8s.artifact_generators import (
    artifact,
    artifact_generators as artifact_generators_factory,
    write_artifact_generators,
)
from cluster.cdk8s.atuin import server as atuin_server, user_provisioner as atuin_user_provisioner
from cluster.cdk8s.authentik import (
    app as authentik_app,
    db as authentik_db,
    db_backups as authentik_db_backups,
    flux_kustomizations as authentik_flux_kustomizations,
    namespace as authentik_namespace,
    proxy_routes as authentik_proxy_routes,
    sso_providers,
)
from cluster.cdk8s.cert_manager import (
    app as cert_manager_app,
    cluster_ca as cert_manager_cluster_ca,
    config as cert_manager_config,
    environment as cert_manager_environment,
    issuer_config as cert_manager_issuer_config,
    trust as cert_manager_trust,
)
from cluster.cdk8s.cli_proxy_api import cli_proxy_api
from cluster.cdk8s.clickhouse import (
    installation as clickhouse_installation,
    operator as clickhouse_operator,
    schema as clickhouse_schema,
)
from cluster.cdk8s.coredns_custom import flux_kustomizations as coredns_custom_flux_kustomizations
from cluster.cdk8s.cpap_sync import app as cpap_sync_app, flux_kustomizations as cpap_sync_flux_kustomizations
from cluster.cdk8s.dcgm_exporter import (
    exporter as dcgm_exporter_exporter,
    flux_kustomizations as dcgm_exporter_flux_kustomizations,
)
from cluster.cdk8s.external_secrets import (
    config as external_secrets_config,
    flux_kustomizations as external_secrets_flux_kustomizations,
    operator as external_secrets_operator,
)
from cluster.cdk8s.flux import health_checks as flux_health_checks
from cluster.cdk8s.flux_grafana_secrets import flux_grafana_secrets
from cluster.cdk8s.flux_image_automation_ghcr import image_automation as flux_image_automation_ghcr
from cluster.cdk8s.flux_webhook import chart as flux_webhook_chart
from cluster.cdk8s.flux_webhook_token import flux_webhook_token
from cluster.cdk8s.forgejo import (
    app as forgejo_app,
    budget_namespace as forgejo_budget_namespace,
    cache as forgejo_cache,
    db as forgejo_db,
    flux_kustomizations as forgejo_flux_kustomizations,
    gitops_modules as forgejo_gitops_modules,
    namespace as forgejo_namespace,
)
from cluster.cdk8s.gaffer_private_source import (
    flux_kustomizations as gaffer_private_source_flux_kustomizations,
    source as gaffer_private_source,
)
from cluster.cdk8s.gatus import app as gatus_app, flux_kustomizations as gatus_flux_kustomizations, sso as gatus_sso
from cluster.cdk8s.github_api_proxy import (
    flux_kustomizations as github_api_proxy_flux_kustomizations,
    proxy as github_api_proxy,
)
from cluster.cdk8s.github_exporter import (
    app as github_exporter_app,
    flux_kustomizations as github_exporter_flux_kustomizations,
)
from cluster.cdk8s.github_secrets_sync import (
    gitops_module as github_secrets_sync_gitops_module,
    secrets as github_secrets_sync_secrets,
)
from cluster.cdk8s.grafana import app as grafana_app, flux_kustomizations as grafana_flux_kustomizations
from cluster.cdk8s.grocy import (
    app as grocy_app,
    flux_kustomizations as grocy_flux_kustomizations,
    mcp as grocy_mcp,
    user_perms as grocy_user_perms,
)
from cluster.cdk8s.haku import (
    charts as haku_charts,
    flux_kustomizations as haku_flux_kustomizations,
    forgejo_tea as haku_forgejo_tea,
    mailbox as haku_mailbox,
    namespace as haku_namespace,
    rbac as haku_rbac,
    ui_image_webhook as haku_ui_image_webhook,
    workloads as haku_workloads,
    workspaces as haku_workspaces,
)
from cluster.cdk8s.haku_ci import flux_kustomizations as haku_ci_flux_kustomizations, runner as haku_ci_runner
from cluster.cdk8s.home_assistant import (
    app as home_assistant_app,
    backup as home_assistant_backup,
    flux_kustomizations as home_assistant_flux_kustomizations,
    namespace as home_assistant_namespace,
)
from cluster.cdk8s.infra_drift import drift_watch
from cluster.cdk8s.kubevirt import (
    app as kubevirt_app,
    cdi as kubevirt_cdi,
    flux_kustomizations as kubevirt_flux_kustomizations,
)
from cluster.cdk8s.kyverno import app as kyverno_app, policies as kyverno_policies
from cluster.cdk8s.langfuse import app as langfuse_app
from cluster.cdk8s.litellm import (
    credentials as litellm_credentials,
    database as litellm_database,
    keys as litellm_keys,
    namespace as litellm_namespace,
    proxy as litellm_proxy,
    secrets as litellm_secrets,
)
from cluster.cdk8s.matrix import matrix, user_provisioner as matrix_user_provisioner
from cluster.cdk8s.monitoring import (
    alloy,
    alloy_otlp_bearer_token,
    cilium_monitoring,
    flux_kustomizations as monitoring_flux_kustomizations,
    grafana_helmrepository,
    grafana_instance,
    grafana_operator,
    loki,
    mimir,
    namespace as monitoring_namespace,
    rules as monitoring_rules,
    stack as monitoring_stack,
    tempo,
)
from cluster.cdk8s.nix_cache import attic as nix_cache_attic, flux_kustomizations as nix_cache_flux_kustomizations
from cluster.cdk8s.oci_cache import flux_kustomizations as oci_cache_flux_kustomizations, zot as oci_cache_zot
from cluster.cdk8s.ollama import app as ollama_app, flux_kustomizations as ollama_flux_kustomizations
from cluster.cdk8s.openebs_lvm import storage as openebs_lvm_storage
from cluster.cdk8s.parked import flux_kustomizations as parked_flux_kustomizations
from cluster.cdk8s.plaid_mcp import app as plaid_mcp_app, db as plaid_mcp_db, reader as plaid_mcp_reader
from cluster.cdk8s.seaweedfs import (
    cluster as seaweedfs_cluster,
    drivefs_artifacts_bucket as seaweedfs_drivefs_artifacts_bucket,
    external_credentials as seaweedfs_external_credentials,
    filer_db as seaweedfs_filer_db,
    flux_kustomizations as seaweedfs_flux_kustomizations,
    forgejo_bucket as seaweedfs_forgejo_bucket,
    loom_gym_bucket as seaweedfs_loom_gym_bucket,
    monitoring as seaweedfs_monitoring,
    namespace as seaweedfs_namespace,
    operator_release as seaweedfs_operator_release,
    pr_visuals_bucket as seaweedfs_pr_visuals_bucket,
    public_coder_agent_backups_bucket as seaweedfs_public_coder_agent_backups_bucket,
    public_s3 as seaweedfs_public_s3,
    registry_cache_bucket as seaweedfs_registry_cache_bucket,
    s3_config as seaweedfs_s3_config,
)
from cluster.cdk8s.seaweedfs_csi import driver as seaweedfs_csi_driver
from cluster.cdk8s.snapshot_controller import flux_kustomizations as snapshot_controller_flux_kustomizations
from cluster.cdk8s.ssh_mcp import generation as ssh_mcp_generation
from cluster.cdk8s.sshpiper_crds import flux_kustomizations as sshpiper_crds_flux_kustomizations
from cluster.cdk8s.study_casino import app as study_casino_app, flux_kustomizations as study_casino_flux_kustomizations
from cluster.cdk8s.tofu_controller import release as tofu_controller_release
from cluster.cdk8s.tofu_state import db as tofu_state_db, namespace as tofu_state_namespace
from cluster.cdk8s.vm_images_publisher import (
    flux_kustomizations as vm_images_publisher_flux_kustomizations,
    publisher as vm_images_publisher_publisher,
)
from cluster.cdk8s.website import website
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
    haku_openclaw_spike_backup.write_manifests(root)
    haku_workloads.write_manifests(root)
    haku_ui_image_webhook.write_manifests(root)
    haku_workspaces.write_manifests(root)
    haku_mailbox.write_manifests(root)
    haku_ci_runner.write_manifests(root)
    agent_workspaces.write_manifests(root)
    alloy_otlp_bearer.write_manifests(root)
    public_coder_agent_config.write_manifests(root)
    public_coder_proxy.write_manifests(root)
    public_coder_sshpiper.write_manifests(root)
    public_coder_backup.write_manifests(root)
    descheduler.write_manifests(root)
    kyverno_app.write_manifests(root)
    kyverno_policies.write_manifests(root)
    stateful_infra.write_seaweedfs_manifests(root)
    seaweedfs_cluster.write_manifests(root)
    egress_fences.write_manifests(root)
    dns_automation.write_manifests(root, mesh)
    litellm_keys.write_manifests(root)
    litellm_namespace.write_manifests(root)
    agentplane_index_workers.write_manifests(root)
    atuin_server.write_manifests(root)
    atuin_user_provisioner.write_manifests(root)
    litellm_database.write_manifests(root)
    litellm_secrets.write_manifests(root)
    forgejo_image_automation.write_manifests(root)
    agents_namespaces.write_manifests(root)
    tofu_state_namespace.write_manifests(root)
    tofu_state_db.write_manifests(root)
    authentik_namespace.write_manifests(root)
    authentik_db.write_manifests(root)
    clickhouse_operator.write_manifests(root)
    clickhouse_installation.write_manifests(root)
    kubevirt_app.write_manifests(root)
    kubevirt_cdi.write_manifests(root)
    cpap_sync_app.write_manifests(root)
    authentik_app.write_manifests(root)
    authentik_proxy_routes.write_manifests(root)
    authentik_db_backups.write_manifests(root)
    forgejo_namespace.write_manifests(root)
    forgejo_db.write_manifests(root)
    forgejo_cache.write_manifests(root)
    home_assistant_namespace.write_manifests(root)
    github_branch_protection.write_manifests(root)
    agent_machine_access.write_manifests(root)
    forgejo_gitops_modules.write_manifests(root)
    alloy_otlp_bearer_token.write_manifests(root)
    monitoring_namespace.write_manifests(root)
    grafana_helmrepository.write_manifests(root)
    flux_grafana_secrets.write_manifests(root)
    drift_watch.write_manifests(root)
    github_secrets_sync_gitops_module.write_manifests(root)
    github_secrets_sync_secrets.write_manifests(root)
    forgejo_images.write_manifests(root)
    gatus_sso.write_manifests(root)
    flux_webhook_token.write_manifests(root)
    sso_providers.write_manifests(root)
    seaweedfs_namespace.write_manifests(root)
    seaweedfs_drivefs_artifacts_bucket.write_manifests(root)
    seaweedfs_loom_gym_bucket.write_manifests(root)
    seaweedfs_forgejo_bucket.write_manifests(root)
    seaweedfs_monitoring.write_manifests(root)
    seaweedfs_public_coder_agent_backups_bucket.write_manifests(root)
    seaweedfs_registry_cache_bucket.write_manifests(root)
    seaweedfs_pr_visuals_bucket.write_manifests(root)
    seaweedfs_operator_release.write_manifests(root)
    seaweedfs_external_credentials.write_manifests(root)
    seaweedfs_filer_db.write_manifests(root)
    seaweedfs_s3_config.write_manifests(root)
    seaweedfs_public_s3.write_manifests(root)
    nix_cache_attic.write_manifests(root)
    vm_images_publisher_publisher.write_manifests(root)
    grafana_operator.write_manifests(root)
    cilium_monitoring.write_manifests(root)
    monitoring_rules.write_manifests(root)
    monitoring_stack.write_manifests(root)
    alloy.write_manifests(root)
    loki.write_manifests(root)
    mimir.write_manifests(root)
    tempo.write_manifests(root)
    grafana_instance.write_manifests(root)
    grafana_app.write_manifests(root)
    github_exporter_app.write_manifests(root)
    langfuse_app.write_manifests(root)
    forgejo_app.write_manifests(root)
    forgejo_budget_namespace.write_manifests(root)
    home_assistant_app.write_manifests(root)
    home_assistant_backup.write_manifests(root)
    grocy_app.write_manifests(root)
    grocy_mcp.write_manifests(root)
    grocy_user_perms.write_manifests(root)
    oci_cache_zot.write_manifests(root)
    plaid_mcp_app.write_manifests(root)
    plaid_mcp_db.write_manifests(root)
    plaid_mcp_reader.write_manifests(root)
    tana_mcp.write_manifests(root)
    haku_egress_proxy.write_manifests(root)
    airlock.write_manifests(root)
    authentik_jwt_rotation.write_manifests(root)
    forgejo_token_rotation.write_manifests(root)
    loki_read_proxy.write_manifests(root)
    claude_sandbox_secrets.write_manifests(root)
    kubectl_passthrough_mcp.write_manifests(root)
    agent_shared_rbac.write_manifests(root)
    website.write_manifests(root)
    ollama_app.write_manifests(root)
    gatus_app.write_manifests(root)
    activitywatch_app.write_manifests(root)
    cli_proxy_api.write_manifests(root)
    matrix.write_manifests(root)
    matrix_user_provisioner.write_manifests(root)
    study_casino_app.write_manifests(root)
    github_api_proxy.write_manifests(root)
    litellm_credentials.write_agentplane_testing_manifests(root)
    cert_manager_app.write_manifests(root)
    cert_manager_trust.write_manifests(root)
    cert_manager_issuer_config.write_manifests(root)
    cert_manager_environment.write_manifests(root)
    cert_manager_config.write_manifests(root)
    cert_manager_cluster_ca.write_manifests(root)
    external_secrets_config.write_manifests(root)
    external_secrets_operator.write_manifests(root)
    ducktape_flux.write_manifests(root)
    flux_webhook_chart.write_manifests(root)
    flux_image_automation_ghcr.write_manifests(root)
    flux_monitoring.write_manifests(root)
    flux_sources.write_manifests(root)
    gaffer_private_source.write_manifests(root)
    tofu_controller_release.write_manifests(root)
    cnpg_operator.write_manifests(root)
    gateway.write_manifests(root)
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
    headlamp.write_manifests(root)
    proxmox_proxy.write_manifests(root)
    volsync.write_manifests(root)
    reloader.write_manifests(root)
    vpa.write_manifests(root)
    node_feature_discovery.write_manifests(root)
    nvidia_device_plugin.write_manifests(root)
    talos_cloud_controller_manager.write_manifests(root)
    kube_api_proxy.write_manifests(root)
    vector_talos_logs.write_manifests(root)
    openebs_lvm_storage.write_manifests(root)
    seaweedfs_csi_driver.write_manifests(root)
    dcgm_exporter_exporter.write_manifests(root)

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
    cert_manager_issuer_config_artifact = artifact("cert-manager-issuer-config", cert_manager_issuer_config.OUTPUT_DIR)
    cert_manager_issuer_config_kustomization = cert_manager_issuer_config.cert_manager_issuer_config(
        flux_chart, cert_manager_issuer_config_artifact
    )
    coredns_custom_artifact = artifact("coredns-custom", "cluster/k8s/coredns-custom")
    coredns_custom_flux_kustomizations.coredns_custom(flux_chart, coredns_custom_artifact)
    external_secrets_crds_kustomization = external_secrets_flux_kustomizations.external_secrets_crds(flux_chart)
    flux_image_automation_ghcr_artifact = artifact("flux-image-automation-ghcr", flux_image_automation_ghcr.OUTPUT_DIR)
    flux_image_automation_ghcr_kustomization = flux_image_automation_ghcr.flux_image_automation_ghcr(
        flux_chart, flux_image_automation_ghcr_artifact
    )
    budget_namespace_artifact = artifact("budget-namespace", forgejo_budget_namespace.OUTPUT_DIR)
    budget_namespace_kustomization = forgejo_budget_namespace.budget_namespace(flux_chart, budget_namespace_artifact)
    haku_namespace_artifact = artifact(haku_namespace.NAME, haku_namespace.OUTPUT_DIR)
    haku_namespace_kustomization = haku_namespace.haku_namespace(flux_chart, haku_namespace_artifact, root)
    hubble_ui_artifact = artifact("hubble-ui", hubble_ui.OUTPUT_DIR)
    hubble_ui.hubble_ui(flux_chart, hubble_ui_artifact)
    kube_api_proxy_artifact = artifact("kube-api-proxy", kube_api_proxy.OUTPUT_DIR)
    kube_api_proxy.kube_api_proxy(flux_chart, kube_api_proxy_artifact)
    kubevirt_cdi_operator_artifact = artifact("kubevirt-cdi-operator", "cluster/k8s/kubevirt/cdi-operator")
    cdi_operator_kustomization = kubevirt_flux_kustomizations.cdi_operator(flux_chart, kubevirt_cdi_operator_artifact)
    kubevirt_operator_artifact = artifact("kubevirt-operator", "cluster/k8s/kubevirt/operator")
    kubevirt_operator_kustomization = kubevirt_flux_kustomizations.kubevirt_operator(
        flux_chart, kubevirt_operator_artifact
    )
    kyverno_artifact = artifact("kyverno", kyverno_app.OUTPUT_DIR)
    kyverno_kustomization = kyverno_app.kyverno(flux_chart, kyverno_artifact)
    local_path_provisioner_artifact = artifact("local-path-provisioner", local_path_provisioner.OUTPUT_DIR)
    local_path_provisioner_kustomization = local_path_provisioner.local_path_provisioner(
        flux_chart, local_path_provisioner_artifact
    )
    monitoring_crds_kustomization = monitoring_flux_kustomizations.monitoring_crds(flux_chart)
    grafana_helmrepository_artifact = artifact("grafana-helmrepository", grafana_helmrepository.OUTPUT_DIR)
    grafana_helmrepository_kustomization = grafana_helmrepository.grafana_helmrepository(
        flux_chart, grafana_helmrepository_artifact
    )
    monitoring_namespace_artifact = artifact("monitoring-namespace", monitoring_namespace.OUTPUT_DIR)
    monitoring_namespace_kustomization = monitoring_namespace.monitoring_namespace(
        flux_chart, monitoring_namespace_artifact
    )
    node_feature_discovery_artifact = artifact("node-feature-discovery", node_feature_discovery.OUTPUT_DIR)
    node_feature_discovery_kustomization = node_feature_discovery.node_feature_discovery(
        flux_chart, node_feature_discovery_artifact
    )
    nvidia_runtimeclass_artifact = artifact("nvidia-runtimeclass", nvidia_runtimeclass.OUTPUT_DIR)
    nvidia_runtimeclass_kustomization = nvidia_runtimeclass.nvidia_runtimeclass(
        flux_chart, nvidia_runtimeclass_artifact
    )
    openebs_lvm_artifact = artifact("openebs-lvm", openebs_lvm_storage.OUTPUT_DIR)
    openebs_lvm_storage.openebs_lvm(flux_chart, openebs_lvm_artifact)
    parked_flux_kustomizations.buildbuddy_executor(flux_chart)
    gecko_namespace_artifact = artifact("gecko-namespace", "cluster/k8s/parked/gecko/namespace")
    gecko_namespace_kustomization = parked_flux_kustomizations.gecko_namespace(flux_chart, gecko_namespace_artifact)
    seaweedfs_namespace_artifact = artifact("seaweedfs-namespace", seaweedfs_namespace.OUTPUT_DIR)
    seaweedfs_namespace_kustomization = seaweedfs_namespace.seaweedfs_namespace(
        flux_chart, seaweedfs_namespace_artifact
    )
    reflector_artifact = artifact("reflector", reflector.OUTPUT_DIR)
    reflector_kustomization = reflector.reflector(flux_chart, reflector_artifact)
    snapshot_controller_crds_kustomization = snapshot_controller_flux_kustomizations.snapshot_controller_crds(
        flux_chart
    )
    sshpiper_crds_kustomization = sshpiper_crds_flux_kustomizations.sshpiper_crds(flux_chart)
    talos_cloud_controller_manager_artifact = artifact(
        "talos-cloud-controller-manager", talos_cloud_controller_manager.OUTPUT_DIR
    )
    talos_cloud_controller_manager.talos_cloud_controller_manager(flux_chart, talos_cloud_controller_manager_artifact)
    user_agentydragon_artifact = artifact("user-agentydragon", user_agentydragon.OUTPUT_DIR)
    user_agentydragon_kustomization = user_agentydragon.user_agentydragon(flux_chart, user_agentydragon_artifact)
    valkey_artifact = artifact("valkey", valkey.OUTPUT_DIR)
    valkey_kustomization = valkey.valkey(flux_chart, valkey_artifact)
    gaffer_private_source_flux_kustomizations.gaffer_private_source(
        flux_chart, flux_image_automation_ghcr_kustomization
    )
    kubevirt_artifact = artifact("kubevirt", kubevirt_app.OUTPUT_DIR)
    kubevirt_kustomization = kubevirt_app.kubevirt(flux_chart, kubevirt_artifact, kubevirt_operator_kustomization)
    haku_rbac_artifact = artifact(haku_rbac.NAME, haku_rbac.OUTPUT_DIR)
    haku_rbac_kustomization = haku_rbac.haku_rbac(flux_chart, haku_rbac_artifact, root, haku_namespace_kustomization)
    descheduler_artifact = artifact("descheduler", descheduler.OUTPUT_DIR)
    descheduler.descheduler(flux_chart, descheduler_artifact, kyverno_kustomization)
    kyverno_policies_artifact = artifact("kyverno-policies", kyverno_policies.OUTPUT_DIR)
    kyverno_policies_kustomization = kyverno_policies.kyverno_policies(
        flux_chart, kyverno_policies_artifact, kyverno_kustomization
    )
    keda_artifact = artifact("keda", keda.OUTPUT_DIR)
    keda_kustomization = keda.keda(flux_chart, keda_artifact, kyverno_kustomization)
    metrics_server_artifact = artifact("metrics-server", metrics_server.OUTPUT_DIR)
    metrics_server_kustomization = metrics_server.metrics_server(
        flux_chart, metrics_server_artifact, kyverno_kustomization
    )
    cdi_artifact = artifact("cdi", kubevirt_cdi.OUTPUT_DIR)
    reloader_artifact = artifact("reloader", reloader.OUTPUT_DIR)
    reloader.reloader(flux_chart, reloader_artifact, kyverno_kustomization)
    cdi_kustomization = kubevirt_flux_kustomizations.cdi(
        flux_chart, cdi_artifact, cdi_operator_kustomization, local_path_provisioner_kustomization
    )
    clickhouse_operator_artifact = artifact("clickhouse-operator", clickhouse_operator.OUTPUT_DIR)
    clickhouse_operator_kustomization = clickhouse_operator.clickhouse_operator(
        flux_chart, clickhouse_operator_artifact, monitoring_crds_kustomization
    )
    flux_monitoring_artifact = artifact("flux-monitoring", flux_monitoring.OUTPUT_DIR)
    flux_monitoring.flux_monitoring(flux_chart, flux_monitoring_artifact, monitoring_crds_kustomization)
    monitoring_cilium_artifact = artifact("monitoring-cilium", cilium_monitoring.OUTPUT_DIR)
    cilium_monitoring.cilium_monitoring(flux_chart, monitoring_cilium_artifact, monitoring_crds_kustomization)
    monitoring_etcd_artifact = artifact("monitoring-etcd", etcd.OUTPUT_DIR)
    etcd.etcd_monitoring(flux_chart, monitoring_etcd_artifact, root, mesh, monitoring_crds_kustomization)
    monitoring_rules_artifact = artifact("monitoring-rules", monitoring_rules.OUTPUT_DIR)
    monitoring_rules.monitoring_rules(flux_chart, monitoring_rules_artifact, monitoring_crds_kustomization)
    grafana_operator_artifact = artifact("grafana-operator", grafana_operator.OUTPUT_DIR)
    grafana_operator_kustomization = grafana_operator.grafana_operator(
        flux_chart, grafana_operator_artifact, monitoring_namespace_kustomization
    )
    nvidia_device_plugin_artifact = artifact("nvidia-device-plugin", nvidia_device_plugin.OUTPUT_DIR)
    nvidia_device_plugin_kustomization = nvidia_device_plugin.nvidia_device_plugin(
        flux_chart,
        nvidia_device_plugin_artifact,
        nvidia_runtimeclass_kustomization,
        node_feature_discovery_kustomization,
    )
    cert_manager_artifact = artifact("cert-manager", cert_manager_app.OUTPUT_DIR)
    cert_manager_kustomization = cert_manager_app.cert_manager(
        flux_chart,
        cert_manager_artifact,
        cert_manager_issuer_config_kustomization,
        reflector_kustomization,
        monitoring_crds_kustomization,
    )
    seaweedfs_operator_artifact = artifact("seaweedfs-operator", seaweedfs_operator_release.OUTPUT_DIR)
    seaweedfs_operator_kustomization = seaweedfs_operator_release.seaweedfs_operator(
        flux_chart, seaweedfs_operator_artifact, seaweedfs_namespace_kustomization
    )
    snapshot_controller_kustomization = snapshot_controller_flux_kustomizations.snapshot_controller(
        flux_chart, snapshot_controller_crds_kustomization
    )
    forgejo_cache_artifact = artifact("forgejo-cache", forgejo_cache.OUTPUT_DIR)
    forgejo_cache.forgejo_cache(
        flux_chart, forgejo_cache_artifact, valkey_kustomization, local_path_provisioner_kustomization
    )
    haku_forgejo_tea_artifact = artifact(haku_forgejo_tea.NAME, haku_forgejo_tea.OUTPUT_DIR)
    haku_forgejo_tea.haku_forgejo_tea(flux_chart, haku_forgejo_tea_artifact, haku_rbac_kustomization)
    claude_rbac_artifact = artifact("claude-rbac", agent_rbac_base.OUTPUT_DIR)
    claude_rbac_kustomization = agent_rbac_base.claude_rbac(
        flux_chart, claude_rbac_artifact, root, kyverno_policies_kustomization
    )
    clickhouse_artifact = artifact("clickhouse", clickhouse_installation.OUTPUT_DIR)
    vpa_artifact = artifact("vpa", vpa.OUTPUT_DIR)
    vpa_kustomization = vpa.vpa(flux_chart, vpa_artifact, kyverno_kustomization, metrics_server_kustomization)
    clickhouse_kustomization = clickhouse_installation.clickhouse(
        flux_chart, clickhouse_artifact, clickhouse_operator_kustomization
    )
    dcgm_exporter_artifact = artifact("dcgm-exporter", "cluster/k8s/dcgm-exporter")
    dcgm_exporter_flux_kustomizations.dcgm_exporter(
        flux_chart, dcgm_exporter_artifact, nvidia_device_plugin_kustomization, monitoring_crds_kustomization
    )
    cert_manager_trust_artifact = artifact("cert-manager-trust", cert_manager_trust.OUTPUT_DIR)
    cert_manager_trust_kustomization = cert_manager_trust.cert_manager_trust(
        flux_chart, cert_manager_trust_artifact, cert_manager_kustomization, kyverno_kustomization
    )
    cnpg_artifact = artifact("cnpg", cnpg_operator.OUTPUT_DIR)
    cnpg_kustomization = cnpg_operator.cnpg(flux_chart, cnpg_artifact, cert_manager_kustomization)
    external_secrets_operator_artifact = artifact("external-secrets-operator", external_secrets_operator.OUTPUT_DIR)
    external_secrets_operator_kustomization = external_secrets_operator.external_secrets_operator(
        flux_chart, external_secrets_operator_artifact, external_secrets_crds_kustomization, cert_manager_kustomization
    )
    external_secrets_config_artifact = artifact("external-secrets-config", external_secrets_config.OUTPUT_DIR)
    external_secrets_config_kustomization = external_secrets_config.external_secrets_config(
        flux_chart, external_secrets_config_artifact, external_secrets_operator_kustomization
    )
    gateway_artifact = artifact("gateway", gateway.OUTPUT_DIR)
    gateway_kustomization = gateway.gateway(
        flux_chart,
        gateway_artifact,
        cert_manager_kustomization,
        kyverno_kustomization,
        cert_manager_issuer_config_kustomization,
    )
    tofu_controller_artifact = artifact("tofu-controller", tofu_controller_release.OUTPUT_DIR)
    tofu_controller_kustomization = tofu_controller_release.tofu_controller(
        flux_chart, tofu_controller_artifact, cert_manager_kustomization, kyverno_kustomization
    )
    volsync_artifact = artifact("volsync", volsync.OUTPUT_DIR)
    volsync_kustomization = volsync.volsync(flux_chart, volsync_artifact, snapshot_controller_kustomization)
    agent_shared_rbac_artifact = artifact("agent-shared-rbac", agent_shared_rbac.OUTPUT_DIR)
    agent_shared_rbac.agent_shared_rbac(
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
        cert_manager_environment.OUTPUT_DIR,
        "cluster/k8s/cert-manager/config",
        "cluster/k8s/cert-manager/cluster-ca",
    )
    cert_manager_environment_kustomization = cert_manager_environment.cert_manager_environment(
        flux_chart,
        cert_manager_environment_artifact,
        cert_manager_kustomization,
        cert_manager_trust_kustomization,
        cert_manager_issuer_config_kustomization,
        external_creds_kustomization,
        external_secrets_config_kustomization,
    )
    seaweedfs_filer_db_artifact = artifact("seaweedfs-filer-db", seaweedfs_filer_db.OUTPUT_DIR)
    seaweedfs_filer_db_kustomization = seaweedfs_filer_db.seaweedfs_filer_db(
        flux_chart, seaweedfs_filer_db_artifact, seaweedfs_namespace_kustomization, cnpg_kustomization
    )
    tofu_state_db_artifact = artifact("tofu-state-db", tofu_state_db.OUTPUT_DIR)
    tofu_state_db_kustomization = tofu_state_db.tofu_state_db(flux_chart, tofu_state_db_artifact, cnpg_kustomization)
    seaweedfs_secrets_artifact = artifact("seaweedfs-secrets", seaweedfs_s3_config.OUTPUT_DIR)
    seaweedfs_secrets_kustomization = seaweedfs_s3_config.seaweedfs_secrets(
        flux_chart,
        seaweedfs_secrets_artifact,
        seaweedfs_namespace_kustomization,
        external_secrets_operator_kustomization,
    )
    website_artifact = artifact("website", website.OUTPUT_DIR)
    website.website(flux_chart, website_artifact, gateway_kustomization)
    proxmox_proxy_artifact = artifact("proxmox-proxy", proxmox_proxy.OUTPUT_DIR)
    proxmox_proxy.proxmox_proxy(flux_chart, proxmox_proxy_artifact, gateway_kustomization)
    kube_system_artifact = artifact("kube-system", kube_system.OUTPUT_DIR)
    kube_system.kube_system(flux_chart, kube_system_artifact, goldilocks_kustomization)
    mitmproxy.agents_mitmproxy(flux_chart, root, cert_manager_trust_kustomization)
    docker_ci_artifact = artifact("docker-ci", "cluster/k8s/parked/docker-ci")
    parked_flux_kustomizations.docker_ci(
        flux_chart, docker_ci_artifact, cert_manager_environment_kustomization, claude_rbac_kustomization
    )
    atuin_artifact = artifact("atuin", atuin_server.OUTPUT_DIR)
    atuin_kustomization = atuin_server.atuin(
        flux_chart, atuin_artifact, cert_manager_issuer_config_kustomization, cnpg_kustomization
    )
    authentik_artifact = artifact("authentik", "cluster/k8s/authentik")
    authentik_kustomization = authentik_flux_kustomizations.authentik(
        flux_chart, authentik_artifact, cnpg_kustomization, monitoring_crds_kustomization
    )
    dns_automation_artifact = artifact("dns-automation", dns_automation.OUTPUT_DIR)
    dns_automation.dns_automation(
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
    drift_watch.infra_drift(
        flux_chart, infra_drift_artifact, tofu_controller_kustomization, tofu_state_db_kustomization
    )
    alloy_otlp_bearer_artifact = artifact("alloy-otlp-bearer", alloy_otlp_bearer.OUTPUT_DIR)
    alloy_otlp_bearer.alloy_otlp_bearer(
        flux_chart,
        alloy_otlp_bearer_artifact,
        external_secrets_config_kustomization,
        claude_rbac_kustomization,
        haku_rbac_kustomization,
    )
    github_secrets_sync_secrets_artifact = artifact(
        "github-secrets-sync-secrets", github_secrets_sync_secrets.OUTPUT_DIR
    )
    github_secrets_sync_secrets_kustomization = github_secrets_sync_secrets.github_secrets_sync_secrets(
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
    atuin_user_provisioner_artifact = artifact("atuin-user-provisioner", atuin_user_provisioner.OUTPUT_DIR)
    atuin_user_provisioner.atuin_user_provisioner(
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
    monitoring_stack_artifact = artifact("monitoring-stack", monitoring_stack.OUTPUT_DIR)
    monitoring_stack.monitoring_stack(
        flux_chart,
        monitoring_stack_artifact,
        monitoring_namespace_kustomization,
        monitoring_crds_kustomization,
        ntfy_kustomization,
        external_secrets_config_kustomization,
    )
    claude_sandbox_secrets_artifact = artifact("claude-sandbox-secrets", claude_sandbox_secrets.OUTPUT_DIR)
    claude_sandbox_secrets.claude_sandbox_secrets(
        flux_chart, claude_sandbox_secrets_artifact, claude_rbac_kustomization, external_secrets_operator_kustomization
    )
    haku_openclaw_spike_backup_artifact = artifact("haku-openclaw-spike-backup", haku_openclaw_spike_backup.OUTPUT_DIR)
    haku_openclaw_spike_backup.haku_openclaw_spike_backup(
        flux_chart, haku_openclaw_spike_backup_artifact, external_secrets_operator_kustomization, volsync_kustomization
    )
    authentik_db_backups_artifact = artifact("authentik-db-backups", authentik_db_backups.OUTPUT_DIR)
    authentik_db_backups.authentik_db_backups(
        flux_chart, authentik_db_backups_artifact, cnpg_kustomization, seaweedfs_cluster_kustomization
    )
    monitoring_loki_artifact = artifact("monitoring-loki", loki.OUTPUT_DIR)
    loki_kustomization = loki.loki(
        flux_chart, monitoring_loki_artifact, grafana_helmrepository_kustomization, seaweedfs_cluster_kustomization
    )
    monitoring_mimir_artifact = artifact("monitoring-mimir", mimir.OUTPUT_DIR)
    mimir_kustomization = mimir.mimir(
        flux_chart,
        monitoring_mimir_artifact,
        monitoring_crds_kustomization,
        grafana_helmrepository_kustomization,
        seaweedfs_cluster_kustomization,
    )
    monitoring_tempo_artifact = artifact("monitoring-tempo", tempo.OUTPUT_DIR)
    tempo.tempo(
        flux_chart,
        monitoring_tempo_artifact,
        monitoring_crds_kustomization,
        grafana_helmrepository_kustomization,
        seaweedfs_cluster_kustomization,
    )
    seaweedfs_drivefs_artifacts_bucket_artifact = artifact(
        "seaweedfs-drivefs-artifacts-bucket", seaweedfs_drivefs_artifacts_bucket.OUTPUT_DIR
    )
    seaweedfs_drivefs_artifacts_bucket_kustomization = (
        seaweedfs_drivefs_artifacts_bucket.seaweedfs_drivefs_artifacts_bucket(
            flux_chart, seaweedfs_drivefs_artifacts_bucket_artifact, seaweedfs_cluster_kustomization
        )
    )
    seaweedfs_external_credentials_artifact = artifact(
        "seaweedfs-external-credentials", seaweedfs_external_credentials.OUTPUT_DIR
    )
    seaweedfs_external_credentials_kustomization = seaweedfs_external_credentials.seaweedfs_external_credentials(
        flux_chart,
        seaweedfs_external_credentials_artifact,
        seaweedfs_secrets_kustomization,
        seaweedfs_cluster_kustomization,
    )
    seaweedfs_forgejo_bucket_artifact = artifact("seaweedfs-forgejo-bucket", seaweedfs_forgejo_bucket.OUTPUT_DIR)
    seaweedfs_forgejo_bucket.seaweedfs_forgejo_bucket(
        flux_chart, seaweedfs_forgejo_bucket_artifact, seaweedfs_cluster_kustomization
    )
    seaweedfs_loom_gym_bucket_artifact = artifact("seaweedfs-loom-gym-bucket", seaweedfs_loom_gym_bucket.OUTPUT_DIR)
    seaweedfs_loom_gym_bucket.seaweedfs_loom_gym_bucket(
        flux_chart, seaweedfs_loom_gym_bucket_artifact, seaweedfs_cluster_kustomization
    )
    seaweedfs_monitoring_artifact = artifact("seaweedfs-monitoring", seaweedfs_monitoring.OUTPUT_DIR)
    seaweedfs_monitoring.seaweedfs_monitoring(
        flux_chart, seaweedfs_monitoring_artifact, seaweedfs_cluster_kustomization, monitoring_crds_kustomization
    )
    seaweedfs_pr_visuals_bucket_artifact = artifact(
        "seaweedfs-pr-visuals-bucket", seaweedfs_pr_visuals_bucket.OUTPUT_DIR
    )
    seaweedfs_pr_visuals_bucket_kustomization = seaweedfs_pr_visuals_bucket.seaweedfs_pr_visuals_bucket(
        flux_chart, seaweedfs_pr_visuals_bucket_artifact, seaweedfs_cluster_kustomization
    )
    seaweedfs_public_coder_agent_backups_bucket_artifact = artifact(
        "seaweedfs-public-coder-agent-backups-bucket", seaweedfs_public_coder_agent_backups_bucket.OUTPUT_DIR
    )
    seaweedfs_public_coder_agent_backups_bucket_kustomization = (
        seaweedfs_public_coder_agent_backups_bucket.seaweedfs_public_coder_agent_backups_bucket(
            flux_chart, seaweedfs_public_coder_agent_backups_bucket_artifact, seaweedfs_cluster_kustomization
        )
    )
    seaweedfs_registry_cache_bucket_artifact = artifact(
        "seaweedfs-registry-cache-bucket", seaweedfs_registry_cache_bucket.OUTPUT_DIR
    )
    seaweedfs_registry_cache_bucket_kustomization = seaweedfs_registry_cache_bucket.seaweedfs_registry_cache_bucket(
        flux_chart, seaweedfs_registry_cache_bucket_artifact, seaweedfs_cluster_kustomization
    )
    seaweedfs_csi_artifact = artifact("seaweedfs-csi", seaweedfs_csi_driver.OUTPUT_DIR)
    seaweedfs_csi_driver.seaweedfs_csi(flux_chart, seaweedfs_csi_artifact, seaweedfs_cluster_kustomization)
    vm_images_publisher_artifact = artifact("vm-images-publisher", "cluster/k8s/vm-images-publisher")
    vm_images_publisher_kustomization = vm_images_publisher_flux_kustomizations.vm_images_publisher(
        flux_chart, vm_images_publisher_artifact, seaweedfs_cluster_kustomization
    )
    kubectl_passthrough_mcp_artifact = artifact("kubectl-passthrough-mcp", kubectl_passthrough_mcp.OUTPUT_DIR)
    kubectl_passthrough_mcp.kubectl_passthrough_mcp(flux_chart, kubectl_passthrough_mcp_artifact)
    forgejo_artifact = artifact("forgejo", "cluster/k8s/forgejo")
    forgejo_kustomization = forgejo_flux_kustomizations.forgejo(
        flux_chart,
        forgejo_artifact,
        cnpg_kustomization,
        external_secrets_operator_kustomization,
        seaweedfs_operator_kustomization,
        monitoring_crds_kustomization,
    )
    matrix_app_artifact = artifact("matrix-app", matrix.OUTPUT_DIR)
    matrix_kustomization = matrix.matrix(flux_chart, matrix_app_artifact, cnpg_kustomization)
    headlamp_app_artifact = artifact("headlamp-app", headlamp.OUTPUT_DIR)
    headlamp.headlamp(flux_chart, headlamp_app_artifact, gateway_kustomization, sso_providers_tf_kustomization)
    grafana_instance_artifact = artifact("grafana-instance", "cluster/k8s/monitoring/grafana-instance")
    grafana_instance_kustomization = monitoring_flux_kustomizations.grafana_instance(
        flux_chart, grafana_instance_artifact, grafana_operator_kustomization, cnpg_kustomization
    )
    gatus_artifact = artifact("gatus", "cluster/k8s/gatus")
    gatus_flux_kustomizations.gatus(flux_chart, gatus_artifact, cnpg_kustomization, monitoring_crds_kustomization)
    flux_webhook_artifact = artifact("flux-webhook", flux_webhook_chart.OUTPUT_DIR)
    flux_webhook_chart.flux_webhook(
        flux_chart,
        flux_webhook_artifact,
        flux_webhook_token_kustomization,
        ntfy_kustomization,
        external_secrets_config_kustomization,
        gateway_kustomization,
    )
    langfuse_artifact = artifact("langfuse", langfuse_app.OUTPUT_DIR)
    langfuse_app.langfuse(
        flux_chart, langfuse_artifact, cnpg_kustomization, valkey_kustomization, seaweedfs_operator_kustomization
    )
    vector_talos_logs_artifact = artifact("vector-talos-logs", vector_talos_logs.OUTPUT_DIR)
    vector_talos_logs.vector_talos_logs(flux_chart, vector_talos_logs_artifact, loki_kustomization)
    monitoring_alloy_artifact = artifact("monitoring-alloy", "cluster/k8s/monitoring/alloy")
    monitoring_flux_kustomizations.alloy(
        flux_chart, monitoring_alloy_artifact, mimir_kustomization, grafana_helmrepository_kustomization
    )
    public_coder_agent_backup_artifact = artifact("public-coder-agent-backup", public_coder_backup.OUTPUT_DIR)
    public_coder_backup.public_coder_agent_backup(
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
    seaweedfs_public_s3_artifact = artifact("seaweedfs-public-s3", seaweedfs_public_s3.OUTPUT_DIR)
    seaweedfs_public_s3_kustomization = seaweedfs_public_s3.seaweedfs_public_s3(
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
    forgejo_images_kustomization = forgejo_images.forgejo_images(
        flux_chart,
        forgejo_images_artifact,
        external_secrets_config_kustomization,
        forgejo_kustomization,
        tofu_controller_kustomization,
        tofu_state_db_kustomization,
    )
    haku_ci_artifact = artifact("haku-ci", "cluster/k8s/haku-ci")
    haku_ci_flux_kustomizations.haku_ci(flux_chart, haku_ci_artifact, keda_kustomization)
    flux_grafana_secrets_artifact = artifact("flux-grafana-secrets", flux_grafana_secrets.OUTPUT_DIR)
    flux_grafana_secrets.flux_grafana_secrets(
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
    agentplane_index_artifact = artifact("agentplane-index", agentplane_index_workers.OUTPUT_DIR)
    agentplane_index_kustomization = agentplane_index_flux_kustomizations.agentplane_index(
        flux_chart,
        agentplane_index_artifact,
        cnpg_kustomization,
        external_secrets_config_kustomization,
        forgejo_images_kustomization,
        local_path_provisioner_kustomization,
        ollama_kustomization,
    )
    airlock_artifact = artifact("airlock", airlock.OUTPUT_DIR)
    agents_flux_kustomizations.airlock(flux_chart, airlock_artifact, external_secrets_operator_kustomization)
    authentik_jwt_rotation_artifact = artifact("authentik-jwt-rotation", authentik_jwt_rotation.OUTPUT_DIR)
    authentik_jwt_rotation_kustomization = agents_flux_kustomizations.authentik_jwt_rotation(
        flux_chart, authentik_jwt_rotation_artifact, external_secrets_operator_kustomization
    )
    loki_read_proxy_artifact = artifact("loki-read-proxy", loki_read_proxy.OUTPUT_DIR)
    loki_read_proxy.loki_read_proxy(flux_chart, loki_read_proxy_artifact, external_secrets_operator_kustomization)
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
    cli_proxy_api_artifact = artifact("cli-proxy-api", cli_proxy_api.OUTPUT_DIR)
    cli_proxy_api_kustomization = cli_proxy_api.cli_proxy_api(
        flux_chart,
        cli_proxy_api_artifact,
        external_secrets_config_kustomization,
        gateway_kustomization,
        cert_manager_environment_kustomization,
        sso_providers_tf_kustomization,
        forgejo_images_kustomization,
    )
    cpap_sync_artifact = artifact("cpap-sync", cpap_sync_app.OUTPUT_DIR)
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
    matrix_user_provisioner_artifact = artifact("matrix-user-provisioner", matrix_user_provisioner.OUTPUT_DIR)
    matrix_user_provisioner.matrix_user_provisioner(
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
    ssh_mcp_generation.ssh_mcp(
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
    litellm_artifact = artifact("litellm", litellm_namespace.OUTPUT_DIR)
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
    forgejo_token_rotation_artifact = artifact("forgejo-token-rotation", forgejo_token_rotation.OUTPUT_DIR)
    agents_flux_kustomizations.forgejo_token_rotation(
        flux_chart,
        forgejo_token_rotation_artifact,
        forgejo_images_kustomization,
        authentik_jwt_rotation_kustomization,
        forgejo_claude_kustomization,
        haku_state_kustomization,
        forgejo_agentydragon_repos_kustomization,
    )
    haku_egress_proxy_artifact = artifact("haku-egress-proxy", haku_egress_proxy.OUTPUT_DIR)
    haku_egress_proxy_kustomization = agents_flux_kustomizations.haku_egress_proxy(
        flux_chart,
        haku_egress_proxy_artifact,
        cert_manager_kustomization,
        cert_manager_trust_kustomization,
        external_secrets_operator_kustomization,
    )
    haku_ui_image_webhook_artifact = artifact("haku-ui-image-webhook", haku_ui_image_webhook.OUTPUT_DIR)
    haku_ui_image_webhook.haku_ui_image_webhook(flux_chart, haku_ui_image_webhook_artifact, haku_state_kustomization)
    haku_workloads_artifact = artifact("haku-workloads", haku_workloads.OUTPUT_DIR)
    haku_workloads.haku_workloads(flux_chart, haku_workloads_artifact, haku_state_kustomization)
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
    haku_workspaces_app_artifact = artifact("haku-workspaces-app", haku_workspaces.OUTPUT_DIR)
    haku_workspaces.haku_workspaces(
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
    agent_workspaces_app_artifact = artifact("agent-workspaces-app", agent_workspaces.OUTPUT_DIR)
    agent_workspaces.agent_workspaces_app(
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
