"""Dispatch manifest generation to each component's local cdk8s helpers."""

import functools
import posixpath
from pathlib import Path

from cdk8s import App

from agentplane.crds.generate import CRD_FILES
from cluster.cdk8s import (
    agent_machine_access,
    agent_namespace_rbac,
    agent_rbac_base,
    agent_sandbox,
    agent_shared_rbac,
    agent_workspaces,
    agentplane_crds,
    aiquota,
    airlock,
    alloy_otlp_bearer,
    authentik_jwt_rotation,
    authentik_tf,
    claude_sandbox_secrets,
    cnpg_operator,
    descheduler,
    dns_automation,
    ducktape_flux,
    egress_fences,
    etcd,
    external_creds,
    external_dns,
    flux,
    flux_monitoring,
    flux_sources,
    forgejo_image_automation,
    forgejo_images,
    forgejo_token_rotation,
    gateway,
    github_branch_protection,
    github_tf,
    goldilocks,
    google_mcp,
    grafana_dashboards,
    ha_mcp,
    haku_egress_proxy,
    headlamp,
    hubble_ui,
    keda,
    kube_api_proxy,
    kube_system,
    kubectl_passthrough_mcp,
    local_path_provisioner,
    loki_read_proxy,
    mcp_oauth_state,
    metrics_server,
    mitmproxy,
    node_feature_discovery,
    ntfy,
    nvidia_device_plugin,
    nvidia_runtimeclass,
    platform_monitoring,
    proxmox_proxy,
    public_coder_agent_config,
    public_coder_backup,
    public_coder_devbox,
    public_coder_proxy,
    public_coder_sshpiper,
    reflector,
    reloader,
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
from cluster.cdk8s.agentplane import binding_delegation, generation as agentplane_generation, staging, testing
from cluster.cdk8s.agentplane_index import workers as agentplane_index_workers
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
    trust as cert_manager_trust,
)
from cluster.cdk8s.claude_session_sync import app as claude_session_sync_app
from cluster.cdk8s.cli_proxy_api import cli_proxy_api
from cluster.cdk8s.clickhouse import (
    installation as clickhouse_installation,
    operator as clickhouse_operator,
    schema as clickhouse_schema,
)
from cluster.cdk8s.cpap_sync import app as cpap_sync_app
from cluster.cdk8s.dcgm_exporter import exporter as dcgm_exporter_exporter
from cluster.cdk8s.external_secrets import (
    config as external_secrets_config,
    flux_kustomizations as external_secrets_flux_kustomizations,
    operator as external_secrets_operator,
)
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
from cluster.cdk8s.gatus import app as gatus_app, sso as gatus_sso
from cluster.cdk8s.generation import write_directory, write_generated_readme
from cluster.cdk8s.github_api_proxy import (
    flux_kustomizations as github_api_proxy_flux_kustomizations,
    proxy as github_api_proxy,
)
from cluster.cdk8s.github_exporter import app as github_exporter_app
from cluster.cdk8s.github_secrets_sync import (
    gitops_module as github_secrets_sync_gitops_module,
    secrets as github_secrets_sync_secrets,
)
from cluster.cdk8s.grafana import app as grafana_app
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
from cluster.cdk8s.haku_ci import runner as haku_ci_runner
from cluster.cdk8s.home_assistant import (
    app as home_assistant_app,
    backup as home_assistant_backup,
    flux_kustomizations as home_assistant_flux_kustomizations,
    namespace as home_assistant_namespace,
)
from cluster.cdk8s.infra_drift import drift_watch
from cluster.cdk8s.kubevirt import app as kubevirt_app, cdi as kubevirt_cdi, operators as kubevirt_operators
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
from cluster.cdk8s.manifest_roots import HAND_WRITTEN_ROOT, PARKED_ROOT
from cluster.cdk8s.matrix import matrix, user_provisioner as matrix_user_provisioner
from cluster.cdk8s.monitoring import (
    alloy,
    alloy_otlp_bearer_token,
    cilium_monitoring,
    flux_kustomizations as monitoring_flux_kustomizations,
    gateway_probe,
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
from cluster.cdk8s.ollama import app as ollama_app
from cluster.cdk8s.openebs_lvm import storage as openebs_lvm_storage
from cluster.cdk8s.parked import (
    augur_evidence as parked_augur_evidence,
    flux_kustomizations as parked_flux_kustomizations,
    haku_openclaw_spike_backup,
    haku_openclaw_spike_config,
    haku_openclaw_spike_proxy,
)
from cluster.cdk8s.plaid_mcp import (
    app as plaid_mcp_app,
    db as plaid_mcp_db,
    pgweb as plaid_mcp_pgweb,
    spend as plaid_mcp_spend,
)
from cluster.cdk8s.seaweedfs import (
    cluster as seaweedfs_cluster,
    drivefs_artifacts_bucket as seaweedfs_drivefs_artifacts_bucket,
    external_credentials as seaweedfs_external_credentials,
    flux_kustomizations as seaweedfs_flux_kustomizations,
    loom_gym_bucket as seaweedfs_loom_gym_bucket,
    monitoring as seaweedfs_monitoring,
    namespace as seaweedfs_namespace,
    operator_release as seaweedfs_operator_release,
    pr_visuals_bucket as seaweedfs_pr_visuals_bucket,
    public_s3 as seaweedfs_public_s3,
)
from cluster.cdk8s.seaweedfs_csi import driver as seaweedfs_csi_driver
from cluster.cdk8s.snapshot_controller import flux_kustomizations as snapshot_controller_flux_kustomizations
from cluster.cdk8s.ssh_mcp import config as ssh_mcp_config, generation as ssh_mcp_generation
from cluster.cdk8s.sshpiper_crds import flux_kustomizations as sshpiper_crds_flux_kustomizations
from cluster.cdk8s.study_casino import app as study_casino_app
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
    write_generated_readme(root)
    mesh = nebula_mesh.load(get_required_path("_main/nebula-mesh.json"))
    devbox_service = public_coder_devbox.write_manifests(root)
    agentplane_staging_resource_chart = agentplane_generation.write_environment_manifests(
        root, staging.ENV, staging.chart
    )
    binding_delegation.write_manifests(root, staging.ENV)
    agentplane_testing_resource_chart = agentplane_generation.write_environment_manifests(
        root, testing.ENV, testing.chart, litellm_credentials.agentplane_testing_chart
    )
    agentplane_staging_health_checks = agentplane_generation.environment_health_checks(
        agentplane_staging_resource_chart, staging.ENV.namespace
    )
    agentplane_testing_health_checks = agentplane_generation.environment_health_checks(
        agentplane_testing_resource_chart, testing.ENV.namespace
    )
    public_coder_agent_config.write_manifests(root)
    public_coder_proxy.write_manifests(root, aiquota_bearer=aiquota.PUBLIC_CODER_BEARER.secret_key)
    ssh_config = ssh_mcp_config.load(devbox_service)
    public_coder_sshpiper.write_manifests(
        root,
        functools.partial(ssh_mcp_generation.sshpiper_pipe_chart, ssh_config=ssh_config),
        app_namespace=public_coder_agent_config.NAMESPACE,
        app_labels=public_coder_agent_config.LABELS,
    )
    seaweedfs_cluster.write_manifests(root)
    litellm_namespace.write_manifests(root)
    litellm_proxy.write_manifests(root)
    litellm_database.write_manifests(root)
    litellm_secrets.write_manifests(root)
    agents_namespaces.write_manifests(root)
    tofu_state_namespace.write_manifests(root)
    tofu_state_db.write_manifests(root)
    mcp_oauth_state.write_manifests(root)
    authentik_namespace.write_manifests(root)
    authentik_db.write_manifests(root)
    authentik_app.write_manifests(root)
    authentik_proxy_routes.write_manifests(root)
    forgejo_namespace.write_manifests(root)
    forgejo_db.write_manifests(root)
    forgejo_cache.write_manifests(root)
    home_assistant_namespace.write_manifests(root)
    vm_images_publisher_publisher.write_manifests(root)
    forgejo_app.write_manifests(root)
    home_assistant_app.write_manifests(root)
    home_assistant_backup.write_manifests(root)
    grocy_mcp.write_manifests(root)
    for household in grocy_app.HOUSEHOLDS:
        grocy_app.write_manifests(root, household, mcp_dir=grocy_mcp.output_dir(household))
    grocy_user_perms.write_manifests(root)
    oci_cache_zot.write_manifests(root)
    plaid_mcp_app.write_manifests(root)
    plaid_mcp_db.write_manifests(root)
    plaid_mcp_pgweb.write_manifests(root)
    plaid_mcp_spend.write_manifests(root)
    tana_mcp.write_manifests(root)
    haku_egress_proxy.write_manifests(root)
    parked_augur_evidence.write_manifests(root)
    activitywatch_app.write_manifests(root)
    github_api_proxy.write_manifests(root)
    ducktape_flux.write_manifests(root)
    flux_sources.write_manifests(root)

    flux_output = root / f"{HAND_WRITTEN_ROOT}/flux"
    flux_output.mkdir(parents=True, exist_ok=True)
    flux_app = App(outdir=str(flux_output))
    flux_chart = flux.kustomizations_chart(flux_app)
    agentplane_crds_artifact = artifact("agentplane-crds", agentplane_crds.OUTPUT_DIR)
    agentplane_crds_kustomization = agentplane_crds.agentplane_crds(
        flux_chart, write_directory(root, agentplane_crds_artifact, siblings=CRD_FILES)
    )
    agent_sandbox_controller_artifact = artifact("agent-sandbox-controller", agent_sandbox.CONTROLLER_DIR)
    agent_sandbox_controller_kustomization = agent_sandbox.agent_sandbox_controller(
        flux_chart,
        write_directory(
            root,
            agent_sandbox_controller_artifact,
            remote_resources=[agent_sandbox.RELEASE],
            patch_charts=[agent_sandbox.controller_patches],
        ),
    )
    artifact_generators_factory(flux_chart)
    external_secrets_crds_kustomization = external_secrets_flux_kustomizations.external_secrets_crds(flux_chart)
    flux_image_automation_ghcr_artifact = artifact("flux-image-automation-ghcr", flux_image_automation_ghcr.OUTPUT_DIR)
    flux_image_automation_ghcr_kustomization = flux_image_automation_ghcr.flux_image_automation_ghcr(
        flux_chart,
        write_directory(root, flux_image_automation_ghcr_artifact, flux_image_automation_ghcr.automation_chart),
    )
    haku_namespace_artifact = artifact(haku_namespace.NAME, haku_namespace.OUTPUT_DIR)
    haku_namespace_kustomization = haku_namespace.haku_namespace(
        flux_chart, write_directory(root, haku_namespace_artifact, haku_namespace.chart)
    )
    kube_api_proxy_artifact = artifact("kube-api-proxy", kube_api_proxy.OUTPUT_DIR)
    kube_api_proxy.kube_api_proxy(flux_chart, write_directory(root, kube_api_proxy_artifact, kube_api_proxy.chart))
    kubevirt_cdi_operator_artifact = artifact("kubevirt-cdi-operator", kubevirt_operators.CDI_OPERATOR_DIR)
    cdi_operator_kustomization = kubevirt_operators.cdi_operator(
        flux_chart,
        write_directory(
            root,
            kubevirt_cdi_operator_artifact,
            remote_resources=[kubevirt_operators.CDI_OPERATOR_RELEASE],
            patch_charts=[kubevirt_operators.cdi_operator_namespace_patch],
            json6902_patches=[kubevirt_operators.CDI_OPERATOR_ON_HIL],
        ),
    )
    kubevirt_operator_artifact = artifact("kubevirt-operator", kubevirt_operators.VIRT_OPERATOR_DIR)
    kubevirt_operator_kustomization = kubevirt_operators.kubevirt_operator(
        flux_chart,
        write_directory(
            root,
            kubevirt_operator_artifact,
            remote_resources=[kubevirt_operators.VIRT_OPERATOR_RELEASE],
            patch_charts=[kubevirt_operators.virt_operator_namespace_patch],
            json6902_patches=[kubevirt_operators.VIRT_OPERATOR_ON_HIL],
        ),
    )
    kyverno_artifact = artifact("kyverno", kyverno_app.OUTPUT_DIR)
    kyverno_kustomization = kyverno_app.kyverno(flux_chart, write_directory(root, kyverno_artifact, kyverno_app.chart))
    local_path_provisioner_artifact = artifact("local-path-provisioner", local_path_provisioner.OUTPUT_DIR)
    local_path_provisioner_kustomization = local_path_provisioner.local_path_provisioner(
        flux_chart, write_directory(root, local_path_provisioner_artifact, local_path_provisioner.chart)
    )
    monitoring_crds_kustomization = monitoring_flux_kustomizations.monitoring_crds(flux_chart)
    grafana_helmrepository_artifact = artifact("grafana-helmrepository", grafana_helmrepository.OUTPUT_DIR)
    grafana_helmrepository.grafana_helmrepository(
        flux_chart, write_directory(root, grafana_helmrepository_artifact, grafana_helmrepository.chart)
    )
    monitoring_namespace_artifact = artifact("monitoring-namespace", monitoring_namespace.OUTPUT_DIR)
    monitoring_namespace_kustomization = monitoring_namespace.monitoring_namespace(
        flux_chart, write_directory(root, monitoring_namespace_artifact, monitoring_namespace.chart)
    )
    node_feature_discovery_artifact = artifact("node-feature-discovery", node_feature_discovery.OUTPUT_DIR)
    node_feature_discovery_kustomization = node_feature_discovery.node_feature_discovery(
        flux_chart, write_directory(root, node_feature_discovery_artifact, node_feature_discovery.chart)
    )
    nvidia_runtimeclass_artifact = artifact("nvidia-runtimeclass", nvidia_runtimeclass.OUTPUT_DIR)
    nvidia_runtimeclass.nvidia_runtimeclass(
        flux_chart, write_directory(root, nvidia_runtimeclass_artifact, nvidia_runtimeclass.chart)
    )
    parked_flux_kustomizations.buildbuddy_executor(flux_chart)
    gecko_namespace_artifact = artifact("gecko-namespace", f"{PARKED_ROOT}/gecko/namespace")
    gecko_namespace_kustomization = parked_flux_kustomizations.gecko_namespace(flux_chart, gecko_namespace_artifact)
    reflector_artifact = artifact("reflector", reflector.OUTPUT_DIR)
    reflector.reflector(flux_chart, write_directory(root, reflector_artifact, reflector.chart))
    snapshot_controller_crds_kustomization = snapshot_controller_flux_kustomizations.snapshot_controller_crds(
        flux_chart
    )
    openebs_lvm_artifact = artifact("openebs-lvm", openebs_lvm_storage.OUTPUT_DIR)
    openebs_lvm_kustomization = openebs_lvm_storage.openebs_lvm(
        flux_chart,
        write_directory(root, openebs_lvm_artifact, openebs_lvm_storage.chart),
        snapshot_controller_crds_kustomization,
    )
    sshpiper_crds_kustomization = sshpiper_crds_flux_kustomizations.sshpiper_crds(flux_chart)
    talos_cloud_controller_manager_artifact = artifact(
        "talos-cloud-controller-manager", talos_cloud_controller_manager.OUTPUT_DIR
    )
    talos_cloud_controller_manager.talos_cloud_controller_manager(
        flux_chart,
        write_directory(
            root,
            talos_cloud_controller_manager_artifact,
            talos_cloud_controller_manager.helmrepository_chart,
            siblings=[talos_cloud_controller_manager.write_helmrelease(root)],
        ),
    )
    user_agentydragon_artifact = artifact("user-agentydragon", user_agentydragon.OUTPUT_DIR)
    user_agentydragon_kustomization = user_agentydragon.user_agentydragon(
        flux_chart,
        # The SOPS sibling turns on Flux decryption.
        write_directory(
            root, user_agentydragon_artifact, user_agentydragon.chart, siblings=["atuin-user-password.sops.yaml"]
        ),
    )
    valkey_artifact = artifact("valkey", valkey.OUTPUT_DIR)
    valkey_kustomization = valkey.valkey(flux_chart, write_directory(root, valkey_artifact, valkey.chart))
    gaffer_private_source_artifact = artifact("gaffer-private-source", gaffer_private_source.OUTPUT_DIR)
    gaffer_private_source.gaffer_private_source(
        flux_chart,
        write_directory(root, gaffer_private_source_artifact, gaffer_private_source.chart),
        flux_image_automation_ghcr_kustomization,
    )
    kubevirt_artifact = artifact("kubevirt", kubevirt_app.OUTPUT_DIR)
    kubevirt_kustomization = kubevirt_app.kubevirt(
        flux_chart, write_directory(root, kubevirt_artifact, kubevirt_app.chart), kubevirt_operator_kustomization
    )
    haku_rbac_artifact = artifact(haku_rbac.NAME, haku_rbac.OUTPUT_DIR)
    haku_rbac_kustomization = haku_rbac.haku_rbac(
        flux_chart, write_directory(root, haku_rbac_artifact, haku_rbac.chart), haku_namespace_kustomization
    )
    descheduler_artifact = artifact("descheduler", descheduler.OUTPUT_DIR)
    descheduler.descheduler(
        flux_chart,
        write_directory(root, descheduler_artifact, descheduler.chart, descheduler.rbac_chart),
        kyverno_kustomization,
    )
    kyverno_policies_artifact = artifact("kyverno-policies", kyverno_policies.OUTPUT_DIR)
    kyverno_policies_kustomization = kyverno_policies.kyverno_policies(
        flux_chart, write_directory(root, kyverno_policies_artifact, *kyverno_policies.CHARTS), kyverno_kustomization
    )
    keda_artifact = artifact("keda", keda.OUTPUT_DIR)
    keda_kustomization = keda.keda(flux_chart, write_directory(root, keda_artifact, keda.chart), kyverno_kustomization)
    metrics_server_artifact = artifact("metrics-server", metrics_server.OUTPUT_DIR)
    metrics_server.metrics_server(
        flux_chart, write_directory(root, metrics_server_artifact, metrics_server.chart), kyverno_kustomization
    )
    cdi_artifact = artifact("cdi", kubevirt_cdi.OUTPUT_DIR)
    reloader_artifact = artifact("reloader", reloader.OUTPUT_DIR)
    reloader.reloader(flux_chart, write_directory(root, reloader_artifact, reloader.chart), kyverno_kustomization)
    kubevirt_cdi.cdi(flux_chart, write_directory(root, cdi_artifact, kubevirt_cdi.chart), cdi_operator_kustomization)
    clickhouse_operator_artifact = artifact("clickhouse-operator", clickhouse_operator.OUTPUT_DIR)
    clickhouse_operator_kustomization = clickhouse_operator.clickhouse_operator(
        flux_chart,
        write_directory(
            root,
            clickhouse_operator_artifact,
            clickhouse_operator.namespace_chart,
            clickhouse_operator.helmrelease_chart,
            siblings=["operator-values.sops.yaml"],
        ),
        monitoring_crds_kustomization,
    )
    platform_monitoring_artifact = artifact("platform-monitoring", platform_monitoring.OUTPUT_DIR)
    platform_monitoring.platform_monitoring(
        flux_chart,
        write_directory(
            root,
            platform_monitoring_artifact,
            flux_monitoring.chart,
            cilium_monitoring.chart,
            lambda app: etcd.chart(app, mesh),
            monitoring_rules.chart,
            seaweedfs_monitoring.chart,
        ),
        monitoring_crds_kustomization,
    )
    monitoring_gateway_probe_artifact = artifact("monitoring-gateway-probe", gateway_probe.OUTPUT_DIR)
    gateway_probe.gateway_probe(
        flux_chart,
        write_directory(
            root,
            monitoring_gateway_probe_artifact,
            functools.partial(gateway_probe.chart, mesh=mesh),
            namespace=gateway_probe.NAMESPACE,
            config_map_generator=[gateway_probe.CONFIG_MAP],
        ),
        monitoring_crds_kustomization,
    )
    grafana_operator_artifact = artifact("grafana-operator", grafana_operator.OUTPUT_DIR)
    grafana_operator_kustomization = grafana_operator.grafana_operator(
        flux_chart,
        write_directory(root, grafana_operator_artifact, grafana_operator.chart),
        monitoring_namespace_kustomization,
    )
    nvidia_device_plugin_artifact = artifact("nvidia-device-plugin", nvidia_device_plugin.OUTPUT_DIR)
    nvidia_device_plugin_kustomization = nvidia_device_plugin.nvidia_device_plugin(
        flux_chart, write_directory(root, nvidia_device_plugin_artifact, nvidia_device_plugin.chart)
    )
    cert_manager_artifact = artifact("cert-manager", cert_manager_app.OUTPUT_DIR)
    cert_manager_kustomization = cert_manager_app.cert_manager(
        flux_chart, write_directory(root, cert_manager_artifact, cert_manager_app.chart), monitoring_crds_kustomization
    )
    seaweedfs_operator_artifact = artifact("seaweedfs-operator", seaweedfs_operator_release.OUTPUT_DIR)
    seaweedfs_operator_kustomization = seaweedfs_operator_release.seaweedfs_operator(
        flux_chart,
        write_directory(root, seaweedfs_operator_artifact, seaweedfs_namespace.chart, seaweedfs_operator_release.chart),
    )
    snapshot_controller_flux_kustomizations.snapshot_controller(flux_chart)
    haku_forgejo_tea_artifact = artifact(haku_forgejo_tea.NAME, haku_forgejo_tea.OUTPUT_DIR)
    haku_forgejo_tea.haku_forgejo_tea(
        flux_chart,
        write_directory(root, haku_forgejo_tea_artifact, siblings=["haku-forgejo-tea.sops.yaml"]),
        haku_rbac_kustomization,
    )
    claude_rbac_artifact = artifact("claude-rbac", agent_rbac_base.OUTPUT_DIR)
    claude_rbac_kustomization = agent_rbac_base.claude_rbac(
        flux_chart, write_directory(root, claude_rbac_artifact, agent_rbac_base.chart), kyverno_policies_kustomization
    )
    clickhouse_artifact = artifact("clickhouse", clickhouse_installation.OUTPUT_DIR)
    vpa_artifact = artifact("vpa", vpa.OUTPUT_DIR)
    vpa.vpa(flux_chart, write_directory(root, vpa_artifact, vpa.chart), kyverno_kustomization)
    clickhouse_kustomization = clickhouse_installation.clickhouse(
        flux_chart,
        write_directory(
            root, clickhouse_artifact, *clickhouse_installation.CHARTS, siblings=clickhouse_installation.SOPS_FILES
        ),
        clickhouse_operator_kustomization,
    )
    dcgm_exporter_artifact = artifact("dcgm-exporter", dcgm_exporter_exporter.OUTPUT_DIR)
    dcgm_exporter_exporter.dcgm_exporter(
        flux_chart,
        write_directory(
            root,
            dcgm_exporter_artifact,
            dcgm_exporter_exporter.chart,
            namespace=dcgm_exporter_exporter.NAMESPACE,
            config_map_generator=[dcgm_exporter_exporter.COUNTERS_CONFIG_MAP],
        ),
        monitoring_crds_kustomization,
    )
    cert_manager_trust_artifact = artifact("cert-manager-trust", cert_manager_trust.OUTPUT_DIR)
    cert_manager_trust_kustomization = cert_manager_trust.cert_manager_trust(
        flux_chart,
        write_directory(root, cert_manager_trust_artifact, cert_manager_trust.chart),
        cert_manager_kustomization,
        kyverno_kustomization,
    )
    cnpg_artifact = artifact("cnpg", cnpg_operator.OUTPUT_DIR)
    cnpg_kustomization = cnpg_operator.cnpg(
        flux_chart, write_directory(root, cnpg_artifact, cnpg_operator.chart), cert_manager_kustomization
    )
    external_secrets_operator_artifact = artifact("external-secrets-operator", external_secrets_operator.OUTPUT_DIR)
    external_secrets_operator_kustomization = external_secrets_operator.external_secrets_operator(
        flux_chart,
        write_directory(root, external_secrets_operator_artifact, external_secrets_operator.chart),
        external_secrets_crds_kustomization,
        cert_manager_kustomization,
    )
    budget_namespace_artifact = artifact("budget-namespace", forgejo_budget_namespace.OUTPUT_DIR)
    forgejo_budget_namespace.budget_namespace(
        flux_chart,
        write_directory(
            root,
            budget_namespace_artifact,
            forgejo_budget_namespace.chart,
            forgejo_budget_namespace.git_credentials_chart,
        ),
        external_secrets_operator_kustomization,
    )
    external_secrets_config_artifact = artifact("external-secrets-config", external_secrets_config.OUTPUT_DIR)
    external_secrets_config.external_secrets_config(
        flux_chart,
        write_directory(root, external_secrets_config_artifact, external_secrets_config.chart),
        external_secrets_operator_kustomization,
    )
    gateway_artifact = artifact("gateway", gateway.OUTPUT_DIR)
    gateway.gateway(
        flux_chart,
        write_directory(root, gateway_artifact, functools.partial(gateway.chart, mesh=mesh)),
        kyverno_kustomization,
    )
    tofu_controller_artifact = artifact("tofu-controller", tofu_controller_release.OUTPUT_DIR)
    tofu_controller_kustomization = tofu_controller_release.tofu_controller(
        flux_chart,
        write_directory(root, tofu_controller_artifact, tofu_controller_release.chart),
        kyverno_kustomization,
    )
    volsync_artifact = artifact("volsync", volsync.OUTPUT_DIR)
    volsync_kustomization = volsync.volsync(
        flux_chart, write_directory(root, volsync_artifact, volsync.chart), monitoring_crds_kustomization
    )
    agent_shared_rbac_artifact = artifact("agent-shared-rbac", agent_shared_rbac.OUTPUT_DIR)
    agent_shared_rbac.agent_shared_rbac(
        flux_chart,
        write_directory(root, agent_shared_rbac_artifact, agent_shared_rbac.chart),
        claude_rbac_kustomization,
    )
    agent_shared_secrets_artifact = artifact("agent-shared-secrets", f"{HAND_WRITTEN_ROOT}/agents/shared-secrets")
    agents_flux_kustomizations.agent_shared_secrets(
        flux_chart,
        write_directory(root, agent_shared_secrets_artifact, siblings=["attic-push-token.sops.yaml"]),
        claude_rbac_kustomization,
    )
    external_creds_artifact = artifact("external-creds", external_creds.OUTPUT_DIR)
    external_creds.external_creds(
        flux_chart,
        # Each credential's SOPS source Secret is a sibling, which turns on Flux decryption.
        write_directory(
            root,
            external_creds_artifact,
            external_creds.chart,
            siblings=[credential.secret_file for credential in external_creds.CREDENTIALS],
        ),
    )
    goldilocks_artifact = artifact("goldilocks", goldilocks.OUTPUT_DIR)
    goldilocks.goldilocks(
        flux_chart, write_directory(root, goldilocks_artifact, goldilocks.chart), kyverno_kustomization
    )
    clickhouse_schema_artifact = artifact("clickhouse-schema", clickhouse_schema.OUTPUT_DIR)
    clickhouse_schema.clickhouse_schema(
        flux_chart,
        write_directory(
            root,
            clickhouse_schema_artifact,
            clickhouse_schema.chart,
            config_map_generator=[clickhouse_schema.write_config_map(root)],
        ),
        clickhouse_kustomization,
    )
    cert_manager_environment_artifact = artifact("cert-manager-environment", cert_manager_environment.OUTPUT_DIR)
    cert_manager_environment.cert_manager_environment(
        flux_chart,
        write_directory(
            root,
            cert_manager_environment_artifact,
            cert_manager_environment.chart,
            cert_manager_config.issuers_chart,
            cert_manager_config.chart,
            cert_manager_cluster_ca.chart,
        ),
        cert_manager_kustomization,
        cert_manager_trust_kustomization,
        external_secrets_operator_kustomization,
    )
    tofu_state_db_artifact = artifact("tofu-state-db", tofu_state_db.OUTPUT_DIR)
    tofu_state_db.tofu_state_db(flux_chart, tofu_state_db_artifact, cnpg_kustomization)
    mcp_oauth_state_artifact = artifact("mcp-oauth-state", mcp_oauth_state.OUTPUT_DIR)
    mcp_oauth_state_kustomization = mcp_oauth_state.mcp_oauth_state_db(
        flux_chart,
        mcp_oauth_state_artifact,
        cnpg_kustomization,
        external_secrets_operator_kustomization,
        monitoring_crds_kustomization,
        kyverno_kustomization,
    )
    website_artifact = artifact("website", website.OUTPUT_DIR)
    website.website(flux_chart, write_directory(root, website_artifact, website.chart), kyverno_kustomization)
    proxmox_proxy_artifact = artifact("proxmox-proxy", proxmox_proxy.OUTPUT_DIR)
    proxmox_proxy_kustomization = proxmox_proxy.proxmox_proxy(
        flux_chart,
        write_directory(
            root, proxmox_proxy_artifact, proxmox_proxy.chart, config_map_generator=[proxmox_proxy.config_map(mesh)]
        ),
        kyverno_kustomization,
    )
    kube_system_artifact = artifact("kube-system", kube_system.OUTPUT_DIR)
    kube_system.kube_system(
        flux_chart,
        write_directory(root, kube_system_artifact, kube_system.chart, hubble_ui.chart),
        kyverno_kustomization,
    )
    agents_mitmproxy_artifact = artifact("agents-mitmproxy", mitmproxy.OUTPUT_DIR)
    write_directory(
        root, agents_mitmproxy_artifact, mitmproxy.namespace_chart, mitmproxy.chart, egress_fences.mitmproxy_cloud_api
    )
    docker_ci_artifact = artifact("docker-ci", f"{PARKED_ROOT}/docker-ci")
    parked_flux_kustomizations.docker_ci(
        flux_chart, docker_ci_artifact, claude_rbac_kustomization, cert_manager_kustomization, kyverno_kustomization
    )
    atuin_artifact = artifact("atuin", atuin_server.OUTPUT_DIR)
    atuin_kustomization = atuin_server.atuin(
        flux_chart, write_directory(root, atuin_artifact, atuin_server.chart), cnpg_kustomization
    )
    authentik_artifact = artifact("authentik", f"{HAND_WRITTEN_ROOT}/authentik")
    authentik_kustomization = authentik_flux_kustomizations.authentik(
        flux_chart, authentik_artifact, cnpg_kustomization, monitoring_crds_kustomization
    )
    gaffer_private_source_flux_kustomizations.gaffer_private_bridge(
        flux_chart, kyverno_kustomization, tofu_controller_kustomization
    )
    dns_automation_artifact = artifact("dns-automation", dns_automation.OUTPUT_DIR)
    dns_automation.dns_automation(
        flux_chart,
        write_directory(root, dns_automation_artifact, functools.partial(dns_automation.chart, mesh=mesh)),
        tofu_controller_kustomization,
        external_secrets_operator_kustomization,
    )
    external_dns_artifact = artifact("external-dns", external_dns.OUTPUT_DIR)
    external_dns.external_dns(
        flux_chart,
        write_directory(root, external_dns_artifact, external_dns.chart),
        external_secrets_operator_kustomization,
    )
    infra_drift_artifact = artifact("infra-drift", drift_watch.OUTPUT_DIR)
    drift_watch.infra_drift(
        flux_chart, write_directory(root, infra_drift_artifact, drift_watch.chart), tofu_controller_kustomization
    )
    alloy_otlp_bearer_artifact = artifact("alloy-otlp-bearer", alloy_otlp_bearer.OUTPUT_DIR)
    alloy_otlp_bearer.alloy_otlp_bearer(
        flux_chart,
        write_directory(
            root, alloy_otlp_bearer_artifact, alloy_otlp_bearer.chart, siblings=[f"{alloy_otlp_bearer.NAME}.sops.yaml"]
        ),
        external_secrets_operator_kustomization,
    )
    github_secrets_sync_secrets_artifact = artifact(
        "github-secrets-sync-secrets", github_secrets_sync_secrets.OUTPUT_DIR
    )
    github_secrets_sync_secrets.github_secrets_sync_secrets(
        flux_chart,
        write_directory(
            root,
            github_secrets_sync_secrets_artifact,
            github_secrets_sync_secrets.chart,
            siblings=["ci-age-key.sops.yaml"],
        ),
        external_secrets_operator_kustomization,
    )
    ntfy_artifact = artifact("ntfy", ntfy.OUTPUT_DIR)
    ntfy.ntfy(
        flux_chart,
        write_directory(root, ntfy_artifact, ntfy.chart, siblings=["credentials.sops.yaml"]),
        cnpg_kustomization,
        external_secrets_operator_kustomization,
        monitoring_crds_kustomization,
        kyverno_kustomization,
    )
    ollama_app_artifact = artifact("ollama-app", ollama_app.OUTPUT_DIR)
    ollama_app.ollama(
        flux_chart,
        # No `namespace=`: that transformer would also rewrite the RoleBinding's `claude-sandbox` subject.
        write_directory(
            root, ollama_app_artifact, ollama_app.chart, config_map_generator=ollama_app.write_config_maps(root)
        ),
        external_secrets_operator_kustomization,
        kyverno_kustomization,
    )
    seaweedfs_cluster_artifact = artifact("seaweedfs-cluster", seaweedfs_cluster.OUTPUT_DIR)
    seaweedfs_flux_kustomizations.seaweedfs_cluster(
        flux_chart,
        seaweedfs_cluster_artifact,
        seaweedfs_operator_kustomization,
        cnpg_kustomization,
        external_secrets_operator_kustomization,
    )
    atuin_user_provisioner_artifact = artifact("atuin-user-provisioner", atuin_user_provisioner.OUTPUT_DIR)
    atuin_user_provisioner.atuin_user_provisioner(
        flux_chart,
        write_directory(root, atuin_user_provisioner_artifact, atuin_user_provisioner.chart),
        atuin_kustomization,
        user_agentydragon_kustomization,
    )
    authentik_tf_artifact = artifact(authentik_tf.NAME, authentik_tf.OUTPUT_DIR)
    authentik_tf.authentik_tf(
        flux_chart,
        write_directory(
            root,
            authentik_tf_artifact,
            sso_providers.chart,
            agent_machine_access.chart,
            gatus_sso.chart,
            alloy_otlp_bearer_token.chart,
        ),
        tofu_controller_kustomization,
    )
    github_tf_artifact = artifact(github_tf.NAME, github_tf.OUTPUT_DIR)
    github_tf.github_tf(
        flux_chart,
        write_directory(
            root,
            github_tf_artifact,
            github_branch_protection.chart,
            github_secrets_sync_gitops_module.chart,
            flux_webhook_token.chart,
        ),
        tofu_controller_kustomization,
    )
    forgejo_gitops_artifact = artifact(forgejo_gitops_modules.NAME, forgejo_gitops_modules.OUTPUT_DIR)
    forgejo_gitops_modules.forgejo_gitops(
        flux_chart,
        write_directory(root, forgejo_gitops_artifact, forgejo_gitops_modules.chart),
        tofu_controller_kustomization,
    )
    monitoring_stack_artifact = artifact("monitoring-stack", monitoring_stack.OUTPUT_DIR)
    monitoring_stack.monitoring_stack(
        flux_chart,
        # The SOPS sibling turns on Flux decryption.
        write_directory(
            root, monitoring_stack_artifact, monitoring_stack.chart, siblings=["grafana-admin-password.sops.yaml"]
        ),
        monitoring_namespace_kustomization,
        monitoring_crds_kustomization,
        kyverno_kustomization,
    )
    claude_sandbox_secrets_artifact = artifact("claude-sandbox-secrets", claude_sandbox_secrets.OUTPUT_DIR)
    claude_sandbox_secrets.claude_sandbox_secrets(
        flux_chart,
        write_directory(
            root,
            claude_sandbox_secrets_artifact,
            claude_sandbox_secrets.chart,
            siblings=claude_sandbox_secrets.SOPS_FILES,
        ),
        claude_rbac_kustomization,
        external_secrets_operator_kustomization,
    )
    haku_openclaw_spike_backup_artifact = artifact("haku-openclaw-spike-backup", haku_openclaw_spike_backup.OUTPUT_DIR)
    # Archived generators remain reproducible; neither directory is a Flux artifact.
    write_directory(
        root, haku_openclaw_spike_backup_artifact, haku_openclaw_spike_backup.chart, siblings=["repository.sops.yaml"]
    )
    authentik_db_backups_artifact = artifact("authentik-db-backups", authentik_db_backups.OUTPUT_DIR)
    authentik_db_backups.authentik_db_backups(
        flux_chart,
        write_directory(root, authentik_db_backups_artifact, authentik_db_backups.chart),
        cnpg_kustomization,
        seaweedfs_operator_kustomization,
    )
    monitoring_loki_artifact = artifact("monitoring-loki", loki.OUTPUT_DIR)
    loki_kustomization = loki.loki(
        flux_chart,
        write_directory(root, monitoring_loki_artifact, loki.chart),
        seaweedfs_operator_kustomization,
        monitoring_crds_kustomization,
    )
    monitoring_mimir_artifact = artifact("monitoring-mimir", mimir.OUTPUT_DIR)
    mimir.mimir(
        flux_chart,
        write_directory(root, monitoring_mimir_artifact, mimir.chart),
        monitoring_crds_kustomization,
        seaweedfs_operator_kustomization,
    )
    monitoring_tempo_artifact = artifact("monitoring-tempo", tempo.OUTPUT_DIR)
    tempo.tempo(
        flux_chart,
        write_directory(root, monitoring_tempo_artifact, tempo.chart),
        monitoring_crds_kustomization,
        seaweedfs_operator_kustomization,
    )
    seaweedfs_drivefs_artifacts_bucket_artifact = artifact(
        "seaweedfs-drivefs-artifacts-bucket", seaweedfs_drivefs_artifacts_bucket.OUTPUT_DIR
    )
    seaweedfs_drivefs_artifacts_bucket.seaweedfs_drivefs_artifacts_bucket(
        flux_chart,
        write_directory(root, seaweedfs_drivefs_artifacts_bucket_artifact, seaweedfs_drivefs_artifacts_bucket.chart),
        seaweedfs_operator_kustomization,
    )
    seaweedfs_external_credentials_artifact = artifact(
        "seaweedfs-external-credentials", seaweedfs_external_credentials.OUTPUT_DIR
    )
    seaweedfs_external_credentials.seaweedfs_external_credentials(
        flux_chart,
        write_directory(
            root,
            seaweedfs_external_credentials_artifact,
            seaweedfs_external_credentials.chart,
            siblings=["claude-reader-credentials.sops.yaml", "drivefs-artifacts-credentials.sops.yaml"],
        ),
        seaweedfs_operator_kustomization,
    )
    seaweedfs_loom_gym_bucket_artifact = artifact("seaweedfs-loom-gym-bucket", seaweedfs_loom_gym_bucket.OUTPUT_DIR)
    seaweedfs_loom_gym_bucket.seaweedfs_loom_gym_bucket(
        flux_chart,
        write_directory(root, seaweedfs_loom_gym_bucket_artifact, seaweedfs_loom_gym_bucket.chart),
        seaweedfs_operator_kustomization,
    )
    seaweedfs_pr_visuals_bucket_artifact = artifact(
        "seaweedfs-pr-visuals-bucket", seaweedfs_pr_visuals_bucket.OUTPUT_DIR
    )
    seaweedfs_pr_visuals_bucket.seaweedfs_pr_visuals_bucket(
        flux_chart,
        write_directory(root, seaweedfs_pr_visuals_bucket_artifact, seaweedfs_pr_visuals_bucket.chart),
        seaweedfs_operator_kustomization,
    )
    seaweedfs_csi_artifact = artifact("seaweedfs-csi", seaweedfs_csi_driver.OUTPUT_DIR)
    seaweedfs_csi_driver.seaweedfs_csi(
        flux_chart, write_directory(root, seaweedfs_csi_artifact, seaweedfs_csi_driver.chart)
    )
    vm_images_publisher_artifact = artifact("vm-images-publisher", vm_images_publisher_publisher.OUTPUT_DIR)
    vm_images_publisher_kustomization = vm_images_publisher_flux_kustomizations.vm_images_publisher(
        flux_chart, vm_images_publisher_artifact, seaweedfs_operator_kustomization
    )
    kubectl_passthrough_mcp_artifact = artifact("kubectl-passthrough-mcp", kubectl_passthrough_mcp.OUTPUT_DIR)
    kubectl_passthrough_mcp.kubectl_passthrough_mcp(
        flux_chart, write_directory(root, kubectl_passthrough_mcp_artifact, kubectl_passthrough_mcp.chart)
    )
    forgejo_artifact = artifact("forgejo", f"{HAND_WRITTEN_ROOT}/forgejo")
    forgejo_flux_kustomizations.forgejo(
        flux_chart,
        forgejo_artifact,
        cnpg_kustomization,
        external_secrets_operator_kustomization,
        seaweedfs_operator_kustomization,
        monitoring_crds_kustomization,
        valkey_kustomization,
    )
    matrix_app_artifact = artifact("matrix-app", matrix.OUTPUT_DIR)
    matrix_kustomization = matrix.matrix(
        flux_chart,
        write_directory(root, matrix_app_artifact, matrix.chart, siblings=matrix.SOPS_FILES),
        cnpg_kustomization,
    )
    headlamp_app_artifact = artifact("headlamp-app", headlamp.OUTPUT_DIR)
    headlamp.headlamp(flux_chart, write_directory(root, headlamp_app_artifact, headlamp.chart))
    grafana_instance_artifact = artifact("grafana-instance", grafana_instance.OUTPUT_DIR)
    grafana_instance.grafana_instance(
        flux_chart,
        write_directory(
            root,
            grafana_instance_artifact,
            grafana_instance.chart,
            config_map_generator=grafana_dashboards.config_map_generator(
                root, grafana_instance.OUTPUT_DIR, grafana_instance.DASHBOARDS
            ),
            configurations=[grafana_dashboards.write_kustomize_config(root, grafana_instance.OUTPUT_DIR)],
        ),
        grafana_operator_kustomization,
        cnpg_kustomization,
    )
    gatus_artifact = artifact("gatus", gatus_app.OUTPUT_DIR)
    gatus_kustomization = gatus_app.gatus(
        flux_chart,
        write_directory(root, gatus_artifact, gatus_app.chart, config_map_generator=[gatus_app.CONFIG_MAP]),
        cnpg_kustomization,
        monitoring_crds_kustomization,
    )
    flux_webhook_artifact = artifact("flux-webhook", flux_webhook_chart.OUTPUT_DIR)
    flux_webhook_chart.flux_webhook(
        flux_chart,
        write_directory(root, flux_webhook_artifact, flux_webhook_chart.chart),
        external_secrets_operator_kustomization,
        kyverno_kustomization,
    )
    langfuse_artifact = artifact("langfuse", langfuse_app.OUTPUT_DIR)
    langfuse_app.langfuse(
        flux_chart,
        write_directory(root, langfuse_artifact, langfuse_app.chart, siblings=["langfuse-secrets.sops.yaml"]),
        cnpg_kustomization,
        valkey_kustomization,
        seaweedfs_operator_kustomization,
    )
    vector_talos_logs_artifact = artifact("vector-talos-logs", vector_talos_logs.OUTPUT_DIR)
    vector_talos_logs.vector_talos_logs(
        flux_chart,
        write_directory(
            root,
            vector_talos_logs_artifact,
            vector_talos_logs.chart,
            namespace=vector_talos_logs.NAMESPACE,
            config_map_generator=[vector_talos_logs.CONFIG_MAP],
        ),
    )
    monitoring_alloy_artifact = artifact("monitoring-alloy", alloy.OUTPUT_DIR)
    alloy.alloy(
        flux_chart,
        write_directory(
            root,
            monitoring_alloy_artifact,
            alloy.chart,
            namespace=alloy.NAMESPACE,
            config_map_generator=[alloy.write_config_map(root)],
        ),
        monitoring_crds_kustomization,
    )
    public_coder_agent_backup_artifact = artifact("public-coder-agent-backup", public_coder_backup.OUTPUT_DIR)
    public_coder_backup.public_coder_agent_backup(
        flux_chart,
        write_directory(
            root, public_coder_agent_backup_artifact, public_coder_backup.chart, siblings=["repository.sops.yaml"]
        ),
        seaweedfs_operator_kustomization,
        external_secrets_operator_kustomization,
        volsync_kustomization,
    )
    oci_cache_artifact = artifact("oci-cache", oci_cache_zot.OUTPUT_DIR)
    oci_cache_kustomization = oci_cache_flux_kustomizations.oci_cache(
        flux_chart,
        oci_cache_artifact,
        valkey_kustomization,
        monitoring_crds_kustomization,
        seaweedfs_operator_kustomization,
    )
    seaweedfs_public_s3_artifact = artifact("seaweedfs-public-s3", seaweedfs_public_s3.OUTPUT_DIR)
    seaweedfs_public_s3.seaweedfs_public_s3(
        flux_chart,
        write_directory(root, seaweedfs_public_s3_artifact, seaweedfs_public_s3.chart),
        seaweedfs_operator_kustomization,
        kyverno_kustomization,
    )
    haku_cloud_agent_artifact = artifact("haku-cloud-agent", f"{PARKED_ROOT}/cloud-agent-tf")
    parked_flux_kustomizations.haku_cloud_agent(
        flux_chart, haku_cloud_agent_artifact, external_secrets_operator_kustomization, tofu_controller_kustomization
    )
    forgejo_images_artifact = artifact("forgejo-images", forgejo_images.OUTPUT_DIR)
    forgejo_images.forgejo_images(
        flux_chart,
        # The SOPS sibling (the tenant's canonical registry credential) turns on Flux decryption.
        write_directory(root, forgejo_images_artifact, forgejo_images.chart, siblings=["registry-creds.sops.yaml"]),
        external_secrets_operator_kustomization,
        tofu_controller_kustomization,
    )
    haku_ci_artifact = artifact("haku-ci", haku_ci_runner.OUTPUT_DIR)
    haku_ci_kustomization = haku_ci_runner.haku_ci(
        flux_chart,
        write_directory(root, haku_ci_artifact, haku_ci_runner.chart),
        keda_kustomization,
        external_secrets_operator_kustomization,
    )
    flux_grafana_secrets_artifact = artifact("flux-grafana-secrets", flux_grafana_secrets.OUTPUT_DIR)
    flux_grafana_secrets.flux_grafana_secrets(
        flux_chart,
        write_directory(root, flux_grafana_secrets_artifact, flux_grafana_secrets.chart),
        grafana_operator_kustomization,
    )
    clickhouse_grafana_artifact = artifact("clickhouse-grafana", grafana_app.OUTPUT_DIR)
    grafana_app.clickhouse_grafana(
        flux_chart,
        write_directory(
            root,
            clickhouse_grafana_artifact,
            grafana_app.chart,
            config_map_generator=grafana_dashboards.config_map_generator(
                root, grafana_app.OUTPUT_DIR, [grafana_app.DASHBOARD]
            ),
            configurations=[grafana_dashboards.write_kustomize_config(root, grafana_app.OUTPUT_DIR)],
        ),
        grafana_operator_kustomization,
    )
    agent_box_artifact = artifact("agent-box", f"{PARKED_ROOT}/agent-box")
    parked_flux_kustomizations.agent_box(
        flux_chart,
        agent_box_artifact,
        kubevirt_kustomization,
        external_secrets_operator_kustomization,
        kyverno_kustomization,
    )
    gecko_artifact = artifact("gecko", f"{PARKED_ROOT}/gecko/app")
    parked_flux_kustomizations.gecko(
        flux_chart,
        gecko_artifact,
        gecko_namespace_kustomization,
        kubevirt_kustomization,
        external_secrets_operator_kustomization,
    )
    activitywatch_artifact = artifact("activitywatch", activitywatch_app.OUTPUT_DIR)
    activitywatch_kustomization = activitywatch_flux_kustomizations.activitywatch(
        flux_chart, activitywatch_artifact, external_secrets_operator_kustomization
    )
    agentplane_index_artifact = artifact(
        "agentplane-index", agentplane_index_workers.OUTPUT_DIR, agentplane_index_workers.PINS_DIR
    )
    agentplane_index_kustomization = agentplane_index_workers.agentplane_index(
        flux_chart,
        write_directory(
            root,
            agentplane_index_artifact,
            agentplane_index_workers.chart,
            components=[posixpath.relpath(agentplane_index_workers.PINS_DIR, agentplane_index_workers.OUTPUT_DIR)],
            config_map_generator=[agentplane_index_workers.CONFIG_MAP],
        ),
        cnpg_kustomization,
        external_secrets_operator_kustomization,
        kyverno_kustomization,
    )
    airlock_artifact = artifact("airlock", airlock.OUTPUT_DIR, airlock.PINS_DIR)
    airlock_directory = write_directory(
        root,
        airlock_artifact,
        airlock.chart,
        siblings=airlock.SOPS_FILES,
        components=[posixpath.relpath(airlock.PINS_DIR, airlock.OUTPUT_DIR)],
        config_map_generator=[airlock.CONFIG_MAP],
    )
    airlock_kustomization = agents_flux_kustomizations.airlock(
        flux_chart, airlock_directory, external_secrets_operator_kustomization
    )
    authentik_jwt_rotation_artifact = artifact(
        "authentik-jwt-rotation", authentik_jwt_rotation.OUTPUT_DIR, authentik_jwt_rotation.PINS_DIR
    )
    authentik_jwt_rotation.authentik_jwt_rotation(
        flux_chart,
        write_directory(
            root,
            authentik_jwt_rotation_artifact,
            authentik_jwt_rotation.chart,
            components=[posixpath.relpath(authentik_jwt_rotation.PINS_DIR, authentik_jwt_rotation.OUTPUT_DIR)],
            config_map_generator=[authentik_jwt_rotation.CONFIG_MAP],
        ),
        external_secrets_operator_kustomization,
    )
    loki_read_proxy_artifact = artifact("loki-read-proxy", loki_read_proxy.OUTPUT_DIR, loki_read_proxy.PINS_DIR)
    loki_read_proxy.loki_read_proxy(
        flux_chart,
        write_directory(
            root,
            loki_read_proxy_artifact,
            loki_read_proxy.chart,
            components=[posixpath.relpath(loki_read_proxy.PINS_DIR, loki_read_proxy.OUTPUT_DIR)],
        ),
        external_secrets_operator_kustomization,
    )
    plaid_mcp_artifact = artifact("plaid-mcp", f"{HAND_WRITTEN_ROOT}/agents/plaid-mcp")
    plaid_mcp_kustomization = agents_flux_kustomizations.plaid_mcp(
        flux_chart,
        plaid_mcp_artifact,
        cnpg_kustomization,
        external_secrets_operator_kustomization,
        authentik_kustomization,
    )
    tana_mcp_artifact = artifact("tana-mcp", tana_mcp.OUTPUT_DIR)
    tana_mcp_kustomization = agents_flux_kustomizations.tana_mcp(
        flux_chart,
        tana_mcp_artifact,
        external_secrets_operator_kustomization,
        mcp_oauth_state_kustomization,
        monitoring_crds_kustomization,
    )
    cli_proxy_api_artifact = artifact("cli-proxy-api", cli_proxy_api.OUTPUT_DIR)
    cli_proxy_api_kustomization = cli_proxy_api.cli_proxy_api(
        flux_chart,
        write_directory(
            root,
            cli_proxy_api_artifact,
            cli_proxy_api.chart,
            siblings=cli_proxy_api.KEY_FILES,
            components=["./image-pins"],
        ),
        external_secrets_operator_kustomization,
        kyverno_kustomization,
    )
    cpap_sync_artifact = artifact("cpap-sync", cpap_sync_app.OUTPUT_DIR)
    cpap_sync_app.cpap_sync(
        flux_chart,
        write_directory(
            root,
            cpap_sync_artifact,
            cpap_sync_app.namespace_chart,
            cpap_sync_app.chart,
            siblings=[cpap_sync_app.CARD_SECRET_FILE],
            components=["./image-pins"],
        ),
        external_secrets_operator_kustomization,
        kubevirt_kustomization,
    )
    claude_session_sync_artifact = artifact(
        "claude-session-sync", claude_session_sync_app.OUTPUT_DIR, claude_session_sync_app.PINS_DIR
    )
    claude_session_sync_app.claude_session_sync(
        flux_chart,
        write_directory(
            root,
            claude_session_sync_artifact,
            claude_session_sync_app.chart,
            components=[posixpath.relpath(claude_session_sync_app.PINS_DIR, claude_session_sync_app.OUTPUT_DIR)],
        ),
        cnpg_kustomization,
        external_secrets_operator_kustomization,
    )
    flux_image_automation_forgejo_artifact = artifact(
        "flux-image-automation-forgejo", forgejo_image_automation.OUTPUT_DIR
    )
    forgejo_image_automation.flux_image_automation_forgejo(
        flux_chart,
        write_directory(root, flux_image_automation_forgejo_artifact, forgejo_image_automation.chart),
        flux_image_automation_ghcr_kustomization,
    )
    github_api_proxy_artifact = artifact("github-api-proxy", f"{HAND_WRITTEN_ROOT}/github-api-proxy")
    github_api_proxy_flux_kustomizations.github_api_proxy(
        flux_chart,
        github_api_proxy_artifact,
        external_secrets_operator_kustomization,
        cert_manager_kustomization,
        monitoring_crds_kustomization,
    )
    github_exporter_artifact = artifact("github-exporter", github_exporter_app.OUTPUT_DIR, github_exporter_app.PINS_DIR)
    github_exporter_app.github_exporter(
        flux_chart,
        write_directory(
            root,
            github_exporter_artifact,
            github_exporter_app.chart,
            components=[posixpath.relpath(github_exporter_app.PINS_DIR, github_exporter_app.OUTPUT_DIR)],
            config_map_generator=grafana_dashboards.config_map_generator(
                root, github_exporter_app.OUTPUT_DIR, [github_exporter_app.DASHBOARD]
            ),
            configurations=[grafana_dashboards.write_kustomize_config(root, github_exporter_app.OUTPUT_DIR)],
        ),
        monitoring_namespace_kustomization,
        monitoring_crds_kustomization,
        external_secrets_operator_kustomization,
        grafana_operator_kustomization,
    )
    grocy_sf_artifact = artifact("grocy-sf", grocy_app.output_dir("sf"), grocy_mcp.output_dir("sf"), grocy_mcp.PINS_DIR)
    grocy_sf_kustomization = grocy_flux_kustomizations.grocy_sf(
        flux_chart,
        grocy_sf_artifact,
        volsync_kustomization,
        external_secrets_operator_kustomization,
        mcp_oauth_state_kustomization,
        monitoring_crds_kustomization,
        kyverno_kustomization,
    )
    grocy_vallejo_artifact = artifact(
        "grocy-vallejo", grocy_app.output_dir("vallejo"), grocy_mcp.output_dir("vallejo"), grocy_mcp.PINS_DIR
    )
    grocy_vallejo_kustomization = grocy_flux_kustomizations.grocy_vallejo(
        flux_chart,
        grocy_vallejo_artifact,
        volsync_kustomization,
        external_secrets_operator_kustomization,
        mcp_oauth_state_kustomization,
        monitoring_crds_kustomization,
        kyverno_kustomization,
    )
    haku_mailbox_artifact = artifact("haku-mailbox", haku_mailbox.OUTPUT_DIR)
    haku_flux_kustomizations.haku_mailbox(
        flux_chart,
        write_directory(
            root,
            haku_mailbox_artifact,
            haku_mailbox.chart,
            siblings=haku_mailbox.SOPS_FILES,
            components=["./image-pins"],
            config_map_generator=[haku_mailbox.CONFIG_MAP, haku_mailbox.INGRESS_CONFIG_MAP],
        ),
        cnpg_kustomization,
        cert_manager_kustomization,
        external_secrets_operator_kustomization,
    )
    home_assistant_artifact = artifact("home-assistant", f"{HAND_WRITTEN_ROOT}/home-assistant")
    home_assistant_flux_kustomizations.home_assistant(
        flux_chart,
        home_assistant_artifact,
        volsync_kustomization,
        external_secrets_operator_kustomization,
        seaweedfs_operator_kustomization,
        monitoring_crds_kustomization,
    )
    matrix_user_provisioner_artifact = artifact(
        "matrix-user-provisioner", matrix_user_provisioner.OUTPUT_DIR, matrix_user_provisioner.PINS_DIR
    )
    matrix_user_provisioner.matrix_user_provisioner(
        flux_chart,
        write_directory(
            root,
            matrix_user_provisioner_artifact,
            matrix_user_provisioner.chart,
            components=[posixpath.relpath(matrix_user_provisioner.PINS_DIR, matrix_user_provisioner.OUTPUT_DIR)],
        ),
        external_secrets_operator_kustomization,
        matrix_kustomization,
    )
    nix_cache_artifact = artifact("nix-cache", nix_cache_attic.OUTPUT_DIR)
    nix_cache_directory = write_directory(
        root,
        nix_cache_artifact,
        nix_cache_attic.chart,
        siblings=["jwt-token.sops.yaml", "cache-keys.sops.yaml"],
        components=["./image-pins"],
        config_map_generator=[nix_cache_attic.SERVER_CONFIG_MAP, nix_cache_attic.ROTATORS_CONFIG_MAP],
    )
    nix_cache_kustomization = nix_cache_flux_kustomizations.nix_cache(
        flux_chart,
        nix_cache_directory,
        cnpg_kustomization,
        external_secrets_operator_kustomization,
        seaweedfs_operator_kustomization,
    )
    sdr_artifact = artifact("sdr", f"{PARKED_ROOT}/sdr")
    parked_flux_kustomizations.sdr(flux_chart, sdr_artifact, external_secrets_operator_kustomization)
    ssh_mcp_artifact = artifact("ssh-mcp", ssh_mcp_generation.OUTPUT_DIR)
    ssh_mcp_generation.ssh_mcp(
        flux_chart,
        write_directory(
            root,
            ssh_mcp_artifact,
            functools.partial(ssh_mcp_generation.chart, ssh_config=ssh_config, mesh=mesh),
            siblings=ssh_mcp_generation.KEY_FILES,
            namespace=ssh_mcp_config.NAMESPACE,
            components=["./image-pins"],
        ),
        external_secrets_operator_kustomization,
    )
    google_mcp_artifact = artifact("google-mcp", google_mcp.OUTPUT_DIR, google_mcp.PINS_DIR)
    google_mcp.google_mcp(
        flux_chart,
        write_directory(
            root,
            google_mcp_artifact,
            google_mcp.chart,
            components=[posixpath.relpath(google_mcp.PINS_DIR, google_mcp.OUTPUT_DIR)],
        ),
        external_secrets_operator_kustomization,
    )
    study_casino_artifact = artifact("study-casino", study_casino_app.OUTPUT_DIR, study_casino_app.PINS_DIR)
    study_casino_kustomization = study_casino_app.study_casino(
        flux_chart,
        write_directory(
            root,
            study_casino_artifact,
            study_casino_app.chart,
            components=[posixpath.relpath(study_casino_app.PINS_DIR, study_casino_app.OUTPUT_DIR)],
            config_map_generator=study_casino_app.config_maps(root),
        ),
        cnpg_kustomization,
        external_secrets_operator_kustomization,
    )
    litellm_artifact = artifact("litellm", litellm_namespace.OUTPUT_DIR)
    litellm_kustomization = litellm_proxy.litellm(
        flux_chart,
        litellm_artifact,
        cnpg_kustomization,
        external_secrets_operator_kustomization,
        monitoring_crds_kustomization,
    )
    aiquota_artifact = artifact("aiquota", aiquota.OUTPUT_DIR)
    aiquota.aiquota(
        flux_chart,
        write_directory(
            root,
            aiquota_artifact,
            aiquota.chart,
            siblings=[f"{aiquota.BEARER_SECRET_NAME}.sops.yaml"],
            components=["./image-pins"],
            config_map_generator=[aiquota.CONFIG_CONFIG_MAP, aiquota.SCHEMA_CONFIG_MAP],
        ),
        external_secrets_operator_kustomization,
        kyverno_kustomization,
    )
    grocy_sf_user_perms_artifact = artifact(
        "grocy-sf-user-perms", grocy_user_perms.output_dir("sf"), grocy_user_perms.BASE_DIR, grocy_user_perms.PINS_DIR
    )
    grocy_flux_kustomizations.grocy_sf_user_perms(flux_chart, grocy_sf_user_perms_artifact, grocy_sf_kustomization)
    grocy_vallejo_user_perms_artifact = artifact(
        "grocy-vallejo-user-perms",
        grocy_user_perms.output_dir("vallejo"),
        grocy_user_perms.BASE_DIR,
        grocy_user_perms.PINS_DIR,
    )
    grocy_flux_kustomizations.grocy_vallejo_user_perms(
        flux_chart, grocy_vallejo_user_perms_artifact, grocy_vallejo_kustomization
    )
    ha_mcp_artifact = artifact("ha-mcp", ha_mcp.OUTPUT_DIR, ha_mcp.PINS_DIR)
    ha_mcp.ha_mcp(
        flux_chart,
        write_directory(
            root, ha_mcp_artifact, ha_mcp.chart, components=[posixpath.relpath(ha_mcp.PINS_DIR, ha_mcp.OUTPUT_DIR)]
        ),
        external_secrets_operator_kustomization,
        monitoring_crds_kustomization,
    )
    forgejo_token_rotation_artifact = artifact(
        "forgejo-token-rotation", forgejo_token_rotation.OUTPUT_DIR, forgejo_token_rotation.PINS_DIR
    )
    forgejo_token_rotation.forgejo_token_rotation(
        flux_chart,
        write_directory(
            root,
            forgejo_token_rotation_artifact,
            forgejo_token_rotation.chart,
            components=[posixpath.relpath(forgejo_token_rotation.PINS_DIR, forgejo_token_rotation.OUTPUT_DIR)],
            config_map_generator=[forgejo_token_rotation.CONFIG_MAP],
        ),
        external_secrets_operator_kustomization,
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
    haku_ui_image_webhook.haku_ui_image_webhook(
        flux_chart,
        write_directory(root, haku_ui_image_webhook_artifact, haku_ui_image_webhook.chart),
        external_secrets_operator_kustomization,
    )
    haku_workloads_artifact = artifact("haku-workloads", haku_workloads.OUTPUT_DIR)
    haku_workloads.haku_workloads(flux_chart, write_directory(root, haku_workloads_artifact, haku_workloads.chart))
    litellm_keys_tf_artifact = artifact("litellm-keys-tf", litellm_keys.OUTPUT_DIR)
    litellm_keys.litellm_keys_tf(
        flux_chart,
        write_directory(
            root, litellm_keys_tf_artifact, litellm_keys.keys_chart, siblings=["litellm-clients-sops-age-key.sops.yaml"]
        ),
        tofu_controller_kustomization,
    )
    haku_openclaw_spike_app_artifact = artifact(
        "haku-openclaw-spike-app", haku_openclaw_spike_config.OUTPUT_DIR, haku_openclaw_spike_config.PINS_DIR
    )
    write_directory(
        root,
        haku_openclaw_spike_app_artifact,
        haku_openclaw_spike_config.namespace_chart,
        haku_openclaw_spike_config.chart,
        haku_openclaw_spike_config.app_chart,
        components=[posixpath.relpath(haku_openclaw_spike_config.PINS_DIR, haku_openclaw_spike_config.OUTPUT_DIR)],
        generator_options=haku_openclaw_spike_config.GENERATOR_OPTIONS,
        config_map_generator=[haku_openclaw_spike_config.write_kubeconfig_config_map(root)],
    )
    write_directory(
        root,
        artifact("parked-haku-openclaw-spike-proxy", haku_openclaw_spike_proxy.OUTPUT_DIR),
        haku_openclaw_spike_proxy.chart,
        egress_fences.haku_openclaw_spike,
        siblings=["openclaw-spike-kube-token.sops.yaml"],
        components=["./image-pins"],
    )
    haku_workspaces_app_artifact = artifact("haku-workspaces-app", haku_workspaces.OUTPUT_DIR)
    haku_workspaces.haku_workspaces(
        flux_chart,
        write_directory(root, haku_workspaces_app_artifact, haku_workspaces.chart),
        haku_rbac_kustomization,
        haku_egress_proxy_kustomization,
        external_secrets_operator_kustomization,
    )
    haku_managed_agent_artifact = artifact("haku-managed-agent", "haku/runtime/x/managed_agent/self_hosted/deploy")
    parked_flux_kustomizations.haku_managed_agent(
        flux_chart,
        haku_managed_agent_artifact,
        external_secrets_operator_kustomization,
        haku_namespace_kustomization,
        haku_rbac_kustomization,
        haku_egress_proxy_kustomization,
    )
    agentplane_testing_artifact = artifact("agentplane-testing", testing.ENV.output_dir, testing.ENV.image_pins)
    agentplane_testing_kustomization = testing.agentplane_testing(
        flux_chart,
        agentplane_testing_artifact,
        agentplane_testing_health_checks,
        agentplane_crds_kustomization,
        agent_sandbox_controller_kustomization,
        cert_manager_trust_kustomization,
        cnpg_kustomization,
        external_secrets_operator_kustomization,
    )
    agent_workspaces_app_artifact = artifact(
        "agent-workspaces-app", agent_workspaces.OUTPUT_DIR, agent_workspaces.PINS_DIR
    )
    write_directory(
        root,
        agent_workspaces_app_artifact,
        agent_workspaces.chart,
        components=[posixpath.relpath(agent_workspaces.PINS_DIR, agent_workspaces.OUTPUT_DIR)],
    )
    parked_flux_kustomizations.haku_dispatch(flux_chart, cnpg_kustomization, external_secrets_operator_kustomization)
    haku_console_artifact = artifact("haku-console", haku_charts.PATH)
    haku_console_kustomization = haku_charts.haku_console(
        flux_chart,
        write_directory(
            root,
            haku_console_artifact,
            haku_charts.console_chart,
            siblings=haku_charts.EXTRA_RESOURCES,
            components=["./image-pins"],
            config_map_generator=haku_charts.CONFIG_MAP_GENERATOR,
        ),
        cnpg_kustomization,
        external_secrets_operator_kustomization,
        monitoring_crds_kustomization,
    )
    agentplane_staging_artifact = artifact("agentplane-staging", staging.ENV.output_dir)
    agentplane_staging_kustomization = staging.agentplane_staging(
        flux_chart,
        agentplane_staging_artifact,
        agentplane_staging_health_checks,
        agentplane_crds_kustomization,
        agent_sandbox_controller_kustomization,
        cert_manager_trust_kustomization,
        cnpg_kustomization,
        external_secrets_operator_kustomization,
    )
    public_coder_agent_app_artifact = artifact(
        "public-coder-agent-app",
        f"{HAND_WRITTEN_ROOT}/agents/public-coder-agent/app",
        public_coder_proxy.OUTPUT_DIR,
        public_coder_sshpiper.OUTPUT_DIR,
    )
    public_coder_agent_app_kustomization = agents_flux_kustomizations.public_coder_agent_app(
        flux_chart,
        public_coder_agent_app_artifact,
        cert_manager_kustomization,
        cert_manager_trust_kustomization,
        external_secrets_operator_kustomization,
        sshpiper_crds_kustomization,
        agentplane_staging_kustomization,
    )
    public_coder_agent_devbox_artifact = artifact("public-coder-agent-devbox", public_coder_devbox.OUTPUT_DIR)
    public_coder_devbox.public_coder_agent_devbox(
        flux_chart, public_coder_agent_devbox_artifact, kubevirt_kustomization, external_secrets_operator_kustomization
    )
    namespace_dependencies = {
        "activitywatch": activitywatch_kustomization,
        "agent-sandbox-system": agent_sandbox_controller_kustomization,
        "agentplane-index": agentplane_index_kustomization,
        "agentplane-testing": agentplane_testing_kustomization,
        "airlock": airlock_kustomization,
        "authentik": authentik_kustomization,
        "cert-manager": cert_manager_kustomization,
        "cli-proxy-api": cli_proxy_api_kustomization,
        "clickhouse": clickhouse_kustomization,
        "cnpg-system": cnpg_kustomization,
        "ducktape-flux": None,
        "haku-console": haku_console_kustomization,
        # Bootstrap roots have no generated owner Kustomization to depend on.
        "flux-system": None,
        "gatus": gatus_kustomization,
        "grocy-sf": grocy_sf_kustomization,
        "grocy-vallejo": grocy_vallejo_kustomization,
        "haku-ci": haku_ci_kustomization,
        "haku-sandbox": haku_rbac_kustomization,
        "litellm": litellm_kustomization,
        "local-path-storage": local_path_provisioner_kustomization,
        "loki": loki_kustomization,
        "monitoring": monitoring_namespace_kustomization,
        "nix-cache": nix_cache_kustomization,
        "node-feature-discovery": node_feature_discovery_kustomization,
        "nvidia-device-plugin": nvidia_device_plugin_kustomization,
        "oci-cache": oci_cache_kustomization,
        "openebs": openebs_lvm_kustomization,
        "plaid-mcp": plaid_mcp_kustomization,
        "proxmox-proxy": proxmox_proxy_kustomization,
        "public-coder-agent": public_coder_agent_app_kustomization,
        "study-casino": study_casino_kustomization,
        "tana-mcp": tana_mcp_kustomization,
        "vm-images-publisher": vm_images_publisher_kustomization,
    }
    binding_delegation.add_flux_kustomizations(flux_chart, staging.ENV, namespace_dependencies)
    agent_namespace_rbac.write_manifests(root)
    agent_namespace_rbac.add_flux_kustomizations(flux_chart, claude_rbac_kustomization)
    # Every artifact built above except the parked nodes': those Kustomizations are suspended.
    write_artifact_generators(
        root,
        ducktape=[
            agentplane_staging_artifact,
            monitoring_stack_artifact,
            ntfy_artifact,
            agentplane_testing_artifact,
            claude_rbac_artifact,
            authentik_artifact,
            authentik_tf_artifact,
            cert_manager_artifact,
            cert_manager_environment_artifact,
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
            seaweedfs_operator_artifact,
            tofu_controller_artifact,
            tofu_state_db_artifact,
            mcp_oauth_state_artifact,
            valkey_artifact,
            kyverno_policies_artifact,
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
            claude_session_sync_artifact,
            cli_proxy_api_artifact,
            clickhouse_operator_artifact,
            clickhouse_schema_artifact,
            cpap_sync_artifact,
            dcgm_exporter_artifact,
            descheduler_artifact,
            dns_automation_artifact,
            external_dns_artifact,
            flux_grafana_secrets_artifact,
            flux_image_automation_forgejo_artifact,
            flux_webhook_artifact,
            forgejo_gitops_artifact,
            budget_namespace_artifact,
            gaffer_private_source_artifact,
            gatus_artifact,
            github_api_proxy_artifact,
            github_tf_artifact,
            github_exporter_artifact,
            goldilocks_artifact,
            google_mcp_artifact,
            grocy_sf_user_perms_artifact,
            grocy_vallejo_user_perms_artifact,
            haku_console_artifact,
            haku_mailbox_artifact,
            haku_forgejo_tea_artifact,
            haku_ui_image_webhook_artifact,
            haku_workloads_artifact,
            haku_workspaces_app_artifact,
            haku_ci_artifact,
            headlamp_app_artifact,
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
            agents_mitmproxy_artifact,
            monitoring_alloy_artifact,
            monitoring_gateway_probe_artifact,
            monitoring_loki_artifact,
            monitoring_mimir_artifact,
            monitoring_tempo_artifact,
            node_feature_discovery_artifact,
            oci_cache_artifact,
            ollama_app_artifact,
            openebs_lvm_artifact,
            platform_monitoring_artifact,
            proxmox_proxy_artifact,
            reloader_artifact,
            seaweedfs_drivefs_artifacts_bucket_artifact,
            seaweedfs_loom_gym_bucket_artifact,
            seaweedfs_pr_visuals_bucket_artifact,
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
