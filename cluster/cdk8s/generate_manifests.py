"""Synthesize each converted directory's manifests with Python cdk8s, writing
them directly into their `cluster/k8s` directory.

Every generated Deployment/Job/CronJob carries a placeholder image tag -- each
environment's own hand-written `image-pins/kustomization.yaml` Kustomize
Component (never written by this generator) carries Flux's `$imagepolicy`
marker and overrides the real tag at `kustomize build` time. See
cluster/docs/cdk8s.md.
"""

from collections.abc import Callable, Sequence
from pathlib import Path
from typing import cast

from cdk8s import ApiObject, App, Chart, Yaml
from cdk8s_plus_34 import ConfigMap
from cilium_crds.io.cilium import (
    CiliumNetworkPolicySpecEgress,
    CiliumNetworkPolicySpecEgressToEndpoints,
    CiliumNetworkPolicySpecEgressToEntities,
    CiliumNetworkPolicySpecEgressToFqdNs,
    CiliumNetworkPolicySpecEgressToPorts,
    CiliumNetworkPolicySpecEgressToPortsPorts,
    CiliumNetworkPolicySpecEgressToPortsPortsProtocol,
)
from flux_kustomize.io.fluxcd.toolkit.kustomize import (
    KustomizationSpec,
    KustomizationSpecDecryption,
    KustomizationSpecDecryptionProvider,
    KustomizationSpecDecryptionSecretRef,
    KustomizationSpecDeletionPolicy,
    KustomizationSpecDependsOn,
    KustomizationSpecHealthCheckExprs,
    KustomizationSpecHealthChecks,
    KustomizationSpecSourceRef,
    KustomizationSpecSourceRefKind,
)

from cluster.cdk8s import haku_openclaw_spike_config, public_coder_agent_config
from cluster.cdk8s.agentplane import (
    actions_constructs,
    actions_settings,
    actions_staging_policies,
    actions_testing_fixtures,
    app_constructs,
    db_constructs,
    dex_constructs,
    egress_constructs,
    llm_ingress_constructs,
    namespace_rbac_constructs,
    replica_profile,
    staging_config,
    testing_config,
)
from cluster.cdk8s.config_format import json5_config, yaml_config
from cluster.cdk8s.flux_constructs import NAMESPACE, flux_kustomization, kustomize_kustomization
from cluster.cdk8s.ha_mcp_constructs import HaMcp
from cluster.cdk8s.litellm_constructs import LiteLLMProxy, LiteLLMServiceMonitor, proxy_specs
from cluster.cdk8s.metadata import metadata
from util.bazel.workspace import get_build_workspace_directory

_LITELLM_APP_DIR = "cluster/k8s/litellm/app"
_HA_MCP_DIR = "cluster/k8s/agents/ha-mcp/app"
_AGENTPLANE_TESTING_DIR = "cluster/k8s/agentplane-testing"
_AGENTPLANE_STAGING_DIR = "cluster/k8s/agentplane-staging"

_AGENTPLANE_STAGING_SPEC = namespace_rbac_constructs.EnvSpec(
    namespace="agentplane-staging",
    description=(
        "Agentplane staging - sandboxed runner Pods (one per Sandbox) and the integration app that drives them."
    ),
)
_AGENTPLANE_TESTING_SPEC = namespace_rbac_constructs.EnvSpec(
    namespace="agentplane-testing",
    description=(
        "Agentplane testing - sandboxed runner Pods (one per Sandbox) and the integration app that drives them."
    ),
    include_action_policy_rule=True,
)
_AGENTPLANE_STAGING_DB_SPEC = db_constructs.DbEnvSpec(
    namespace="agentplane-staging", instances=2, pod_anti_affinity=True
)
_AGENTPLANE_TESTING_DB_SPEC = db_constructs.DbEnvSpec(
    namespace="agentplane-testing", instances=1, pod_anti_affinity=False
)
_STAGING_REPLICAS = replica_profile.STAGING
_TESTING_REPLICAS = replica_profile.TESTING
_AGENTPLANE_STAGING_LLM_INGRESS_SPEC = llm_ingress_constructs.LlmIngressEnvSpec(
    namespace="agentplane-staging",
    replicas=_STAGING_REPLICAS.replicas,
    strategy=_STAGING_REPLICAS.strategy,
    topology_spread=_STAGING_REPLICAS.topology_spread,
    litellm_key_secret_name="litellm-key-agentplane-staging",
)
_AGENTPLANE_TESTING_LLM_INGRESS_SPEC = llm_ingress_constructs.LlmIngressEnvSpec(
    namespace="agentplane-testing",
    replicas=_TESTING_REPLICAS.replicas,
    strategy=_TESTING_REPLICAS.strategy,
    topology_spread=_TESTING_REPLICAS.topology_spread,
    litellm_key_secret_name="litellm-key-cheap-experiments",
)
_AGENTPLANE_STAGING_EGRESS_SPEC = egress_constructs.EgressEnvSpec(
    namespace="agentplane-staging",
    ca_secret_name="agentplane-egress-ca",
    replicas=_STAGING_REPLICAS.replicas,
    strategy=_STAGING_REPLICAS.strategy,
    min_ready=_STAGING_REPLICAS.min_ready,
    topology_spread=_STAGING_REPLICAS.topology_spread,
    pdb_min_available=_STAGING_REPLICAS.pdb_min_available,
)
_AGENTPLANE_TESTING_EGRESS_SPEC = egress_constructs.EgressEnvSpec(
    namespace="agentplane-testing",
    ca_secret_name="agentplane-testing-egress-ca",
    replicas=_TESTING_REPLICAS.replicas,
    strategy=_TESTING_REPLICAS.strategy,
    min_ready=_TESTING_REPLICAS.min_ready,
    topology_spread=_TESTING_REPLICAS.topology_spread,
    pdb_min_available=_TESTING_REPLICAS.pdb_min_available,
)
_AGENTPLANE_STAGING_APP_SPEC = app_constructs.AppEnvSpec(
    namespace="agentplane-staging",
    replicas=_STAGING_REPLICAS.replicas,
    strategy=_STAGING_REPLICAS.strategy,
    min_ready=_STAGING_REPLICAS.min_ready,
    topology_spread=_STAGING_REPLICAS.topology_spread,
    pdb_min_available=_STAGING_REPLICAS.pdb_min_available,
    hostname="agentplane-staging.allegedly.works",
    oidc_issuer="https://auth.allegedly.works/application/o/agentplane/",
    reach_incluster_authentik=True,
    runner_zone="hil-ovh",
    runner_ca_configmap_name=_AGENTPLANE_STAGING_EGRESS_SPEC.ca_secret_name,
)
_AGENTPLANE_TESTING_APP_SPEC = app_constructs.AppEnvSpec(
    namespace="agentplane-testing",
    replicas=_TESTING_REPLICAS.replicas,
    strategy=_TESTING_REPLICAS.strategy,
    min_ready=_TESTING_REPLICAS.min_ready,
    topology_spread=_TESTING_REPLICAS.topology_spread,
    pdb_min_available=_TESTING_REPLICAS.pdb_min_available,
    hostname="agentplane-testing.allegedly.works",
    oidc_issuer="https://agentplane-dex-testing.allegedly.works/dex",
    reach_incluster_authentik=False,
    runner_zone=None,
    runner_ca_configmap_name=_AGENTPLANE_TESTING_EGRESS_SPEC.ca_secret_name,
)
_AGENTPLANE_STAGING_ACTION_FEDERATION = {
    "mode": "exchange",
    "service_url": (
        f"http://agentplane-actions.agentplane-staging.svc.cluster.local:{actions_constructs.CONTAINER_PORT}"
    ),
    "token_endpoint": "https://auth.allegedly.works/application/o/token/",
    "login_jwks_uri": "https://auth.allegedly.works/application/o/agentplane/jwks/",
    "target": {
        "issuer": "https://auth.allegedly.works/application/o/agentplane-actions/",
        "audience": "agentplane-actions",
        "jwks_uri": "https://auth.allegedly.works/application/o/agentplane-actions/jwks/",
    },
    "scope": "openid",
}
_AGENTPLANE_STAGING_OPERATOR_OIDC = {
    "issuer": "https://auth.allegedly.works/application/o/agentplane-actions/",
    "audience": "agentplane-actions",
    "jwks_uri": "https://auth.allegedly.works/application/o/agentplane-actions/jwks/",
}
_AGENTPLANE_TESTING_ACTION_FEDERATION = {
    "mode": "direct",
    "service_url": (
        f"http://agentplane-actions.agentplane-testing.svc.cluster.local:{actions_constructs.CONTAINER_PORT}"
    ),
    "login_jwks_uri": "https://agentplane-dex-testing.allegedly.works/dex/keys",
    "login_token_profile": "dex",
    "target": {
        "issuer": "https://agentplane-dex-testing.allegedly.works/dex",
        "audience": "agentplane-testing",
        "jwks_uri": "https://agentplane-dex-testing.allegedly.works/dex/keys",
        "token_profile": "dex",
    },
    "scope": "openid",
}
_AGENTPLANE_TESTING_OPERATOR_OIDC = {
    "issuer": "https://agentplane-dex-testing.allegedly.works/dex",
    "audience": "agentplane-testing",
    "jwks_uri": "https://agentplane-dex-testing.allegedly.works/dex/keys",
    "token_profile": "dex",
}
_AGENTPLANE_STAGING_ACTIONS_EXTRA_EGRESS = [
    CiliumNetworkPolicySpecEgress(
        to_fqd_ns=[
            CiliumNetworkPolicySpecEgressToFqdNs(match_name=host) for host in actions_settings.WEB_PUSH_ALLOWED_HOSTS
        ],
        to_ports=[
            CiliumNetworkPolicySpecEgressToPorts(
                ports=[
                    CiliumNetworkPolicySpecEgressToPortsPorts(
                        port="443", protocol=CiliumNetworkPolicySpecEgressToPortsPortsProtocol.TCP
                    )
                ],
                server_names=list(actions_settings.WEB_PUSH_ALLOWED_HOSTS),
            )
        ],
    ),
    CiliumNetworkPolicySpecEgress(
        to_endpoints=[
            CiliumNetworkPolicySpecEgressToEndpoints(
                match_labels={"k8s:io.kubernetes.pod.namespace": "ssh-mcp", "app.kubernetes.io/name": "ssh-mcp"}
            )
        ],
        to_ports=[
            CiliumNetworkPolicySpecEgressToPorts(
                ports=[
                    CiliumNetworkPolicySpecEgressToPortsPorts(
                        port="8080", protocol=CiliumNetworkPolicySpecEgressToPortsPortsProtocol.TCP
                    )
                ]
            )
        ],
    ),
    # Same public-origin Gateway path as the BFF: only Authentik SNI on node:443. The
    # resolver fetches /application/o/agentplane-actions/jwks/ over HTTPS.
    CiliumNetworkPolicySpecEgress(
        to_entities=[
            CiliumNetworkPolicySpecEgressToEntities.REMOTE_HYPHEN_NODE,
            CiliumNetworkPolicySpecEgressToEntities.HOST,
        ],
        to_ports=[
            CiliumNetworkPolicySpecEgressToPorts(
                ports=[
                    CiliumNetworkPolicySpecEgressToPortsPorts(
                        port="443", protocol=CiliumNetworkPolicySpecEgressToPortsPortsProtocol.TCP
                    )
                ],
                server_names=["auth.allegedly.works"],
            )
        ],
    ),
    # GitHub MCP discovery advertises github.com as its OAuth authorization server.
    CiliumNetworkPolicySpecEgress(
        to_fqd_ns=[
            CiliumNetworkPolicySpecEgressToFqdNs(match_name=host) for host in ("api.githubcopilot.com", "github.com")
        ],
        to_ports=[
            CiliumNetworkPolicySpecEgressToPorts(
                ports=[
                    CiliumNetworkPolicySpecEgressToPortsPorts(
                        port="443", protocol=CiliumNetworkPolicySpecEgressToPortsPortsProtocol.TCP
                    )
                ],
                server_names=["api.githubcopilot.com", "github.com"],
            )
        ],
    ),
    # `github_public_repository` policies confirm a repository is public with an
    # unauthenticated GitHub REST call (github_policy/visibility.py); no credential
    # rides this path.
    CiliumNetworkPolicySpecEgress(
        to_fqd_ns=[CiliumNetworkPolicySpecEgressToFqdNs(match_name="api.github.com")],
        to_ports=[
            CiliumNetworkPolicySpecEgressToPorts(
                ports=[
                    CiliumNetworkPolicySpecEgressToPortsPorts(
                        port="443", protocol=CiliumNetworkPolicySpecEgressToPortsPortsProtocol.TCP
                    )
                ],
                server_names=["api.github.com"],
            )
        ],
    ),
    # The Kubernetes MCP server uses the public Gateway/remote-node path.
    CiliumNetworkPolicySpecEgress(
        to_entities=[
            CiliumNetworkPolicySpecEgressToEntities.REMOTE_HYPHEN_NODE,
            CiliumNetworkPolicySpecEgressToEntities.HOST,
        ],
        to_ports=[
            CiliumNetworkPolicySpecEgressToPorts(
                ports=[
                    CiliumNetworkPolicySpecEgressToPortsPorts(
                        port="443", protocol=CiliumNetworkPolicySpecEgressToPortsPortsProtocol.TCP
                    )
                ],
                server_names=["kubectl-passthrough-mcp.allegedly.works"],
            )
        ],
    ),
    # Gateway Service traffic is checked against the selected backend, with the
    # client's original SNI. See cluster/docs/cilium_network_policy.md § Egress
    # through the Gateway Service.
    CiliumNetworkPolicySpecEgress(
        to_endpoints=[
            CiliumNetworkPolicySpecEgressToEndpoints(
                match_labels={
                    "k8s:io.kubernetes.pod.namespace": "authentik",
                    "app.kubernetes.io/name": "authentik",
                    "app.kubernetes.io/instance": "authentik",
                    "app.kubernetes.io/component": "server",
                }
            )
        ],
        to_ports=[
            CiliumNetworkPolicySpecEgressToPorts(
                ports=[
                    CiliumNetworkPolicySpecEgressToPortsPorts(
                        port="9000", protocol=CiliumNetworkPolicySpecEgressToPortsPortsProtocol.TCP
                    )
                ],
                server_names=["auth.allegedly.works"],
            )
        ],
    ),
]
_AGENTPLANE_TESTING_ACTIONS_EXTRA_EGRESS = [
    # The direct federation verifier fetches Dex's JWKS over the public-origin Gateway path.
    CiliumNetworkPolicySpecEgress(
        to_entities=[
            CiliumNetworkPolicySpecEgressToEntities.REMOTE_HYPHEN_NODE,
            CiliumNetworkPolicySpecEgressToEntities.HOST,
        ],
        to_ports=[
            CiliumNetworkPolicySpecEgressToPorts(
                ports=[
                    CiliumNetworkPolicySpecEgressToPortsPorts(
                        port="443", protocol=CiliumNetworkPolicySpecEgressToPortsPortsProtocol.TCP
                    )
                ],
                server_names=["agentplane-dex-testing.allegedly.works"],
            )
        ],
    ),
    # MCP OAuth discovery/token exchange/tool calls for the linked "example" fixture:
    # cluster-internal only, unlike the real GitHub/Kubernetes MCP OAuth providers
    # linked in staging.
    CiliumNetworkPolicySpecEgress(
        to_endpoints=[
            CiliumNetworkPolicySpecEgressToEndpoints(
                match_labels={
                    "k8s:io.kubernetes.pod.namespace": "agentplane-testing",
                    "app.kubernetes.io/name": "agentplane-oauth-fixture",
                }
            )
        ],
        to_ports=[
            CiliumNetworkPolicySpecEgressToPorts(
                ports=[
                    CiliumNetworkPolicySpecEgressToPortsPorts(
                        port="8080", protocol=CiliumNetworkPolicySpecEgressToPortsPortsProtocol.TCP
                    )
                ]
            )
        ],
    ),
]
# What each environment's Flux Kustomization waits on. Staging additionally federates
# operator login through the shared Authentik (sso-providers-tf) and reaches ssh-mcp.
_AGENTPLANE_DEPENDS_ON = (
    "agentplane-crds",
    "agent-sandbox-controller",
    "cert-manager-environment",
    "cert-manager-trust",
    "claude-rbac",
    "cnpg",
    "external-creds",
    "external-secrets-config",
    "forgejo-images",
    "gateway",
    "litellm-keys-tf",
    "local-path-provisioner",
    "reflector",
)
_AGENTPLANE_STAGING_DEPENDS_ON = (*_AGENTPLANE_DEPENDS_ON, "sso-providers-tf", "ssh-mcp")
# The chart objects whose readiness gates the environment, in the order the checks are
# listed. The trust-manager Bundle writes its target ConfigMap asynchronously, outside
# the rendered input, so that ConfigMap is checked explicitly rather than via `wait`.
_HEALTH_CHECK_KINDS = ("Namespace", "Cluster", "Database", "Deployment", "Certificate", "Bundle")
_CNPG_DATABASE_READY = (
    "has(status.applied) && status.applied && "
    "has(status.observedGeneration) && status.observedGeneration == metadata.generation"
)
_AGENTPLANE_STAGING_ACTIONS_SPEC = actions_constructs.ActionsEnvSpec(
    namespace="agentplane-staging",
    replicas=_STAGING_REPLICAS.replicas,
    strategy=_STAGING_REPLICAS.strategy,
    min_ready=_STAGING_REPLICAS.min_ready,
    topology_spread=_STAGING_REPLICAS.topology_spread,
    pdb_min_available=_STAGING_REPLICAS.pdb_min_available,
    hostname="agentplane-actions-staging.allegedly.works",
    settings=actions_settings.staging_settings(),
    action_federation=_AGENTPLANE_STAGING_ACTION_FEDERATION,
    action_federation_description="OIDC federation configuration for the Agentplane app and Action Service",
    operator_oidc=_AGENTPLANE_STAGING_OPERATOR_OIDC,
    extra_reload_secrets=(
        "haku-console-github-mcp-client-credentials",
        "agentplane-staging-web-push-vapid",
        "ssh-mcp-bearer",
    ),
    oauth_secret_items=("client-secret", "jwt-signing-key", "encryption-key"),
    web_push_secret_name="agentplane-staging-web-push-vapid",
    github_mcp_client_secret_name="haku-console-github-mcp-client-credentials",
    ssh_mcp_bearer=True,
    extra_egress=_AGENTPLANE_STAGING_ACTIONS_EXTRA_EGRESS,
)
_AGENTPLANE_TESTING_ACTIONS_SPEC = actions_constructs.ActionsEnvSpec(
    namespace="agentplane-testing",
    replicas=_TESTING_REPLICAS.replicas,
    strategy=_TESTING_REPLICAS.strategy,
    min_ready=_TESTING_REPLICAS.min_ready,
    topology_spread=_TESTING_REPLICAS.topology_spread,
    pdb_min_available=_TESTING_REPLICAS.pdb_min_available,
    hostname="agentplane-actions-testing.allegedly.works",
    settings=actions_settings.testing_settings(),
    action_federation=_AGENTPLANE_TESTING_ACTION_FEDERATION,
    action_federation_description="Direct Dex operator federation pins for the isolated testing Action Service.",
    operator_oidc=_AGENTPLANE_TESTING_OPERATOR_OIDC,
    extra_egress=_AGENTPLANE_TESTING_ACTIONS_EXTRA_EGRESS,
)
_HAKU_OPENCLAW_SPIKE_APP_DIR = "cluster/k8s/agents/haku-openclaw-spike/app"
_PUBLIC_CODER_AGENT_APP_DIR = "cluster/k8s/agents/public-coder-agent/app"


def _write_yaml(path: Path, manifest: dict[str, object]) -> None:
    path.write_text(Yaml.format_objects([manifest]))


def _generate_litellm_app(root: Path) -> None:
    (spec,) = proxy_specs()  # only one LiteLLM proxy today; extend proxy_specs() when a second lands

    app_dir = root / _LITELLM_APP_DIR
    app_dir.mkdir(parents=True, exist_ok=True)
    app = App(outdir=str(app_dir))
    chart = Chart(app, spec.name, disable_resource_name_hashes=True)
    LiteLLMProxy(chart, "proxy", spec)
    LiteLLMServiceMonitor(chart, "monitoring")
    app.synth()

    _write_yaml(
        app_dir / "flux-kustomization.yaml",
        flux_kustomization(
            "litellm",
            spec=KustomizationSpec(
                interval="10m",
                path=f"./{_LITELLM_APP_DIR}",
                prune=True,
                source_ref=KustomizationSpecSourceRef(
                    kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name="litellm", namespace=NAMESPACE
                ),
                timeout="10m",
                depends_on=[
                    KustomizationSpecDependsOn(name=dep)
                    for dep in (
                        "external-secrets-config",
                        "forgejo-images",
                        "litellm-secrets",
                        "litellm-db",
                        "gateway",
                        "cert-manager-environment",
                        "langfuse-secrets",
                        "reflector",
                        "tana-mcp",
                        # The ServiceMonitor/PodMonitor CRD (folded in from the retired
                        # litellm-servicemonitor Kustomization, #7103).
                        "monitoring-crds",
                    )
                ],
            ),
        ),
    )
    _write_yaml(
        app_dir / "kustomization.yaml",
        kustomize_kustomization(namespace="litellm", resources=[f"{spec.name}.k8s.yaml"], components=["./image-pins"]),
    )


def _generate_ha_mcp(root: Path) -> None:
    name = "ha-mcp"
    app_dir = root / _HA_MCP_DIR
    app_dir.mkdir(parents=True, exist_ok=True)
    app = App(outdir=str(app_dir))
    chart = Chart(app, name, disable_resource_name_hashes=True)
    HaMcp(chart, "ha-mcp")
    app.synth()

    _write_yaml(
        app_dir / "flux-kustomization.yaml",
        flux_kustomization(
            name,
            spec=KustomizationSpec(
                retry_interval="1m",
                interval="10m",
                timeout="5m",
                source_ref=KustomizationSpecSourceRef(
                    kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace=NAMESPACE
                ),
                path=f"./{_HA_MCP_DIR}",
                prune=True,
                wait=True,
                health_checks=[
                    KustomizationSpecHealthChecks(
                        api_version="batch/v1", kind="Job", name="ha-mcp-token-provisioner", namespace="home-assistant"
                    ),
                    KustomizationSpecHealthChecks(api_version="apps/v1", kind="Deployment", name=name, namespace=name),
                ],
                # bearer.sops.yaml (hand-written, stays alongside this generated output --
                # see cluster/docs/cdk8s.md) is SOPS-encrypted; without this Flux applies the
                # ENC[...] ciphertext literally and the facade rejects every call from haku-console.
                decryption=KustomizationSpecDecryption(
                    provider=KustomizationSpecDecryptionProvider.SOPS,
                    secret_ref=KustomizationSpecDecryptionSecretRef(name="sops-age-cluster-secrets"),
                ),
                depends_on=[
                    KustomizationSpecDependsOn(name=dep)
                    for dep in (
                        "external-secrets-config",
                        "forgejo-images",
                        "home-assistant",
                        "monitoring-crds",  # the ServiceMonitor CRD
                    )
                ],
            ),
        ),
    )
    _write_yaml(
        app_dir / "kustomization.yaml",
        # bearer.sops.yaml stays hand-written; this generated file just lists it as a plain
        # sibling resource -- cdk8s never touches its bytes. See cluster/docs/cdk8s.md.
        kustomize_kustomization(resources=[f"{name}.k8s.yaml", "bearer.sops.yaml"], components=["./image-pins"]),
    )


def _build_agentplane_chart(
    app: App,
    *,
    namespace_rbac_spec: namespace_rbac_constructs.EnvSpec,
    app_config_data: dict[str, str],
    db_spec: db_constructs.DbEnvSpec,
    llm_ingress_spec: llm_ingress_constructs.LlmIngressEnvSpec,
    egress_spec: egress_constructs.EgressEnvSpec,
    app_spec: app_constructs.AppEnvSpec,
    actions_spec: actions_constructs.ActionsEnvSpec,
    add_extra: Callable[[Chart], None],
) -> Chart:
    """Build the environment's entire Namespace/RBAC/app-config/workload surface
    (Namespace, ResourceQuota, LimitRange, operator RBAC, the model-catalog ConfigMap,
    db, llm-ingress, egress, app, actions, and via `add_extra` either staging's
    ActionPolicySet/Binding objects and claude-ai ServiceAccount or testing's
    mcp-everything/oauth-fixture fixtures and Dex) as one chart, without synthesizing it
    -- shared by `_generate_agentplane` (writes it to disk) and tests (synth it in memory
    via `cdk8s.Testing`, see `testing_chart`/`staging_chart`, instead of reading it back
    off a committed file).
    """
    chart = Chart(app, "agentplane", disable_resource_name_hashes=True)
    namespace_rbac_constructs.NamespaceQuota(chart, "namespace", namespace_rbac_spec)
    namespace_rbac_constructs.AgentRbac(chart, "rbac", namespace_rbac_spec)
    ConfigMap(
        chart, "config", metadata=metadata("agentplane-app-config", namespace_rbac_spec.namespace), data=app_config_data
    )
    db_constructs.Db(chart, "db", db_spec)
    llm_ingress_constructs.LlmIngress(chart, "llm-ingress", llm_ingress_spec)
    egress_constructs.Egress(chart, "egress", egress_spec)
    app_constructs.App(chart, "app", app_spec)
    actions_constructs.Actions(chart, "actions", actions_spec)
    add_extra(chart)
    return chart


def testing_chart(app: App) -> Chart:
    """agentplane-testing's full chart, for in-memory synth (`cdk8s.Testing.synth`) in
    tests -- the exact same specs `generate_manifests()` writes to disk with."""
    return _build_agentplane_chart(
        app,
        namespace_rbac_spec=_AGENTPLANE_TESTING_SPEC,
        app_config_data={"config.yaml": yaml_config(testing_config.config())},
        db_spec=_AGENTPLANE_TESTING_DB_SPEC,
        llm_ingress_spec=_AGENTPLANE_TESTING_LLM_INGRESS_SPEC,
        egress_spec=_AGENTPLANE_TESTING_EGRESS_SPEC,
        app_spec=_AGENTPLANE_TESTING_APP_SPEC,
        actions_spec=_AGENTPLANE_TESTING_ACTIONS_SPEC,
        add_extra=_add_testing_extra,
    )


def staging_chart(app: App) -> Chart:
    """agentplane-staging's full chart -- see `testing_chart`."""
    return _build_agentplane_chart(
        app,
        namespace_rbac_spec=_AGENTPLANE_STAGING_SPEC,
        app_config_data={"config.yaml": yaml_config(staging_config.config())},
        db_spec=_AGENTPLANE_STAGING_DB_SPEC,
        llm_ingress_spec=_AGENTPLANE_STAGING_LLM_INGRESS_SPEC,
        egress_spec=_AGENTPLANE_STAGING_EGRESS_SPEC,
        app_spec=_AGENTPLANE_STAGING_APP_SPEC,
        actions_spec=_AGENTPLANE_STAGING_ACTIONS_SPEC,
        add_extra=actions_staging_policies.add_staging_action_policies,
    )


def _agentplane_health_checks(chart: Chart, namespace: str) -> list[KustomizationSpecHealthChecks]:
    # `Chart.api_objects` is direct children only; every object here sits inside a Construct.
    api_objects = [cast(ApiObject, node) for node in chart.node.find_all() if ApiObject.is_api_object(node)]
    objects = sorted(
        (obj for obj in api_objects if obj.kind in _HEALTH_CHECK_KINDS),
        key=lambda obj: _HEALTH_CHECK_KINDS.index(obj.kind),
    )
    checks = [
        KustomizationSpecHealthChecks(
            api_version=obj.api_version, kind=obj.kind, name=obj.name, namespace=obj.metadata.namespace
        )
        for obj in objects
    ]
    # trust-manager names a Bundle's target ConfigMap after the Bundle.
    checks.extend(
        KustomizationSpecHealthChecks(api_version="v1", kind="ConfigMap", name=obj.name, namespace=namespace)
        for obj in objects
        if obj.kind == "Bundle"
    )
    return checks


def _generate_agentplane(
    root: Path,
    env_dir: str,
    *,
    namespace: str,
    description: str,
    depends_on: Sequence[str],
    chart_builder: Callable[[App], Chart],
    extra_resources: Sequence[str] = (),
) -> None:
    """Synthesize `chart_builder`'s output (`staging_chart`/`testing_chart`) into
    `env_dir` as a single `agentplane.k8s.yaml`. Single failure domain by design --
    including the CNPG Postgres `Cluster` -- accepted for both non-production
    environments.

    Also (re)writes `env_dir`'s Flux Kustomization (health checks derived from the
    chart's own objects) and its root Kustomization, just this one generated file plus
    `extra_resources` (staging's hand-written `web-push-vapid.sops.yaml`). The sibling
    image-pins/ Component stays hand-written, same as litellm/ha-mcp.
    """
    out_dir = root / env_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    app = App(outdir=str(out_dir))
    chart = chart_builder(app)
    app.synth()

    _write_yaml(
        out_dir / "flux-kustomization.yaml",
        flux_kustomization(
            namespace,
            description=description,
            spec=KustomizationSpec(
                retry_interval="1m",
                interval="10m",
                timeout="10m",
                path=f"./{env_dir}",
                prune=True,
                # This one Kustomization owns the CNPG Cluster's PVCs; pruning on
                # deletion would take the database with them.
                deletion_policy=KustomizationSpecDeletionPolicy.ORPHAN,
                health_checks=_agentplane_health_checks(chart, namespace),
                health_check_exprs=[
                    KustomizationSpecHealthCheckExprs(
                        api_version="postgresql.cnpg.io/v1", kind="Database", current=_CNPG_DATABASE_READY
                    )
                ],
                decryption=(
                    KustomizationSpecDecryption(
                        provider=KustomizationSpecDecryptionProvider.SOPS,
                        secret_ref=KustomizationSpecDecryptionSecretRef(name="sops-age-cluster-secrets"),
                    )
                    if any(resource.endswith(".sops.yaml") for resource in extra_resources)
                    else None
                ),
                source_ref=KustomizationSpecSourceRef(
                    kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=namespace, namespace=NAMESPACE
                ),
                depends_on=[KustomizationSpecDependsOn(name=dep) for dep in depends_on],
            ),
        ),
    )
    _write_yaml(
        out_dir / "kustomization.yaml",
        kustomize_kustomization(resources=["agentplane.k8s.yaml", *extra_resources], components=["./image-pins"]),
    )


def _build_config_map_chart(
    app: App, *, chart_name: str, configmap_name: str, namespace: str, data: dict[str, str]
) -> Chart:
    """Build a single-ConfigMap chart without synthesizing it -- shared by
    `_write_config_map_chart` (writes it to disk) and tests (in-memory synth)."""
    chart = Chart(app, chart_name, disable_resource_name_hashes=True)
    ConfigMap(chart, "config", metadata=metadata(configmap_name, namespace), data=data)
    return chart


def _write_config_map_chart(root: Path, app_dir: str, chart_builder: Callable[[App], Chart]) -> None:
    """Synthesize `chart_builder`'s output into `app_dir`'s existing, otherwise
    hand-written Kustomization -- see cluster/docs/cdk8s.md's "SOPS secrets in a
    converted directory" for the general pattern of a directory mixing generated and
    hand-written files. `flux-kustomization.yaml`/`kustomization.yaml` stay hand-written;
    only this one ConfigMap's content is generated, replacing what used to be a Kustomize
    `configMapGenerator` entry.
    """
    out_dir = root / app_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    app = App(outdir=str(out_dir))
    chart_builder(app)
    app.synth()


def _haku_openclaw_spike_config_chart(app: App) -> Chart:
    return _build_config_map_chart(
        app,
        chart_name="haku-openclaw-spike-config",
        configmap_name="haku-openclaw-spike-config",
        namespace="haku-openclaw-spike",
        data={
            "openclaw.json": json5_config(haku_openclaw_spike_config.config()),
            "claude.json": json5_config(haku_openclaw_spike_config.claude_config()),
        },
    )


def _generate_haku_openclaw_spike_config(root: Path) -> None:
    _write_config_map_chart(root, _HAKU_OPENCLAW_SPIKE_APP_DIR, _haku_openclaw_spike_config_chart)


def _public_coder_agent_config_chart(app: App) -> Chart:
    return _build_config_map_chart(
        app,
        chart_name="public-coder-agent-config",
        configmap_name="public-coder-agent-config",
        namespace="public-coder-agent",
        data={"openclaw.json5": json5_config(public_coder_agent_config.config())},
    )


def _generate_public_coder_agent_config(root: Path) -> None:
    _write_config_map_chart(root, _PUBLIC_CODER_AGENT_APP_DIR, _public_coder_agent_config_chart)


def _add_testing_extra(chart: Chart) -> None:
    actions_testing_fixtures.add_testing_fixtures(chart)
    # Testing-only: staging federates directly to the shared Authentik instead.
    dex_constructs.Dex(chart, "dex")


def generate_manifests(root: Path) -> None:
    """Write every converted directory's generated manifests under `root`."""
    _generate_litellm_app(root)
    _generate_ha_mcp(root)
    _generate_agentplane(
        root,
        _AGENTPLANE_STAGING_DIR,
        namespace=_AGENTPLANE_STAGING_SPEC.namespace,
        description=(
            "Complete Agentplane staging environment, including namespace, database, egress, LLM ingress, "
            "Actions, app, runner template, and operator RBAC."
        ),
        depends_on=_AGENTPLANE_STAGING_DEPENDS_ON,
        chart_builder=staging_chart,
        extra_resources=["web-push-vapid.sops.yaml"],
    )
    _generate_agentplane(
        root,
        _AGENTPLANE_TESTING_DIR,
        namespace=_AGENTPLANE_TESTING_SPEC.namespace,
        description=(
            "Complete Agentplane testing environment, including namespace, database, Dex, egress, LLM ingress, "
            "Actions fixtures, app, runner template, and operator RBAC."
        ),
        depends_on=_AGENTPLANE_DEPENDS_ON,
        chart_builder=testing_chart,
    )
    _generate_haku_openclaw_spike_config(root)
    _generate_public_coder_agent_config(root)


def main() -> None:
    generate_manifests(get_build_workspace_directory())


if __name__ == "__main__":
    main()
