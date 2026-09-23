"""Airlock's namespace, identity, session secret, Service, route and ingress policy
(cluster/k8s/agents/airlock).

Hand-written beside the output (`kustomization.yaml` lists both): `deployment.yaml`, whose
`AIRLOCK_IMAGE_TAG` env value carries a Flux image-automation marker that an `images:`
Component cannot set; the SOPS client credentials; and `config.yaml`, rendered into the
Deployment's ConfigMap.
"""

from __future__ import annotations

from pathlib import Path

from cdk8s import ApiObjectMetadata, App, Chart
from cdk8s_plus_34 import k8s
from cilium_crds.io.cilium import (
    CiliumNetworkPolicySpecIngress,
    CiliumNetworkPolicySpecIngressFromEntities,
    CiliumNetworkPolicySpecIngressToPorts,
    CiliumNetworkPolicySpecIngressToPortsPorts,
    CiliumNetworkPolicySpecIngressToPortsPortsProtocol,
)
from eso_password_generator_crds.io.external_secrets.generators import Password, PasswordSpec
from external_secrets_clusterexternalsecret_crds.io.external_secrets import (
    ClusterExternalSecret,
    ClusterExternalSecretSpec,
    ClusterExternalSecretSpecExternalSecretSpec,
    ClusterExternalSecretSpecExternalSecretSpecDataFrom,
    ClusterExternalSecretSpecExternalSecretSpecDataFromExtract,
    ClusterExternalSecretSpecExternalSecretSpecSecretStoreRef,
    ClusterExternalSecretSpecExternalSecretSpecSecretStoreRefKind,
    ClusterExternalSecretSpecExternalSecretSpecTarget,
)
from external_secrets_crds.io.external_secrets import (
    ExternalSecret,
    ExternalSecretSpec,
    ExternalSecretSpecDataFrom,
    ExternalSecretSpecDataFromSourceRef,
    ExternalSecretSpecDataFromSourceRefGeneratorRef,
    ExternalSecretSpecDataFromSourceRefGeneratorRefKind,
    ExternalSecretSpecRefreshPolicy,
    ExternalSecretSpecTarget,
    ExternalSecretSpecTargetCreationPolicy,
    ExternalSecretSpecTargetTemplate,
)

from cluster.cdk8s import cilium
from cluster.cdk8s.forgejo_images import forgejo_images_creds_external_secret
from cluster.cdk8s.gateway import https_route
from cluster.cdk8s.generation import write_charts
from cluster.cdk8s.metadata import metadata

NAME = "airlock"
OUTPUT_DIR = "cluster/k8s/agents/airlock"
# The hand-written deployment.yaml's pod labels.
_LABELS = {"app.kubernetes.io/name": NAME, "app.kubernetes.io/component": "server"}
_PORT = 8765
_SESSION_SECRET = "airlock-session-secret"
_SECRET_WRITER = "airlock-secret-writer"


def _session_secret(chart: Chart) -> None:
    """Airlock's session-signing key is app-local. A key rotation invalidates existing browser
    sessions but does not need to update Authentik or another credential store."""
    generator = Password(
        chart,
        "session-secret-generator",
        metadata=metadata(_SESSION_SECRET, NAME),
        spec=PasswordSpec(allow_repeat=True, digits=16, length=64, no_upper=False, symbols=0),
    )
    ExternalSecret(
        chart,
        "session-secret",
        metadata=metadata(_SESSION_SECRET, NAME),
        spec=ExternalSecretSpec(
            data_from=[
                ExternalSecretSpecDataFrom(
                    source_ref=ExternalSecretSpecDataFromSourceRef(
                        generator_ref=ExternalSecretSpecDataFromSourceRefGeneratorRef(
                            api_version="generators.external-secrets.io/v1alpha1",
                            kind=ExternalSecretSpecDataFromSourceRefGeneratorRefKind.PASSWORD,
                            name=generator.name,
                        )
                    )
                )
            ],
            refresh_policy=ExternalSecretSpecRefreshPolicy.CREATED_ONCE,
            target=ExternalSecretSpecTarget(
                creation_policy=ExternalSecretSpecTargetCreationPolicy.ORPHAN,
                immutable=True,
                name=_SESSION_SECRET,
                template=ExternalSecretSpecTargetTemplate(data={"session-secret": "{{ .password }}"}, type="Opaque"),
            ),
        ),
    )


def _mirror(chart: Chart, name: str, namespaces: list[str]) -> None:
    """Mirror the Secret `name` Airlock writes into the `airlock` namespace into `namespaces`."""
    ClusterExternalSecret(
        chart,
        name,
        metadata=ApiObjectMetadata(name=name),
        spec=ClusterExternalSecretSpec(
            namespaces=namespaces,
            external_secret_spec=ClusterExternalSecretSpecExternalSecretSpec(
                refresh_interval="1m",
                secret_store_ref=ClusterExternalSecretSpecExternalSecretSpecSecretStoreRef(
                    name="kubernetes-airlock-secret-store",
                    kind=ClusterExternalSecretSpecExternalSecretSpecSecretStoreRefKind.CLUSTER_SECRET_STORE,
                ),
                target=ClusterExternalSecretSpecExternalSecretSpecTarget(name=name),
                data_from=[
                    ClusterExternalSecretSpecExternalSecretSpecDataFrom(
                        extract=ClusterExternalSecretSpecExternalSecretSpecDataFromExtract(key=name)
                    )
                ],
            ),
        ),
    )


def _ingress_from(entity: CiliumNetworkPolicySpecIngressFromEntities) -> CiliumNetworkPolicySpecIngress:
    return CiliumNetworkPolicySpecIngress(
        from_entities=[entity],
        to_ports=[
            CiliumNetworkPolicySpecIngressToPorts(
                ports=[
                    CiliumNetworkPolicySpecIngressToPortsPorts(
                        port=str(_PORT), protocol=CiliumNetworkPolicySpecIngressToPortsPortsProtocol.TCP
                    )
                ]
            )
        ],
    )


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    forgejo_images_creds_external_secret(chart, "forgejo-images-creds", namespace=NAME)
    k8s.KubeNamespace(
        chart,
        "namespace",
        metadata=k8s.ObjectMeta(
            name=NAME,
            labels={
                "name": NAME,
                "goldilocks.fairwinds.com/enabled": "true",
                "goldilocks.fairwinds.com/vpa-update-mode": "auto",
                "rbac.ducktape.io/agent-readable-logs": "true",
            },
            annotations={
                # controlledValues has to be spelled out here: default-vpa-requests-only
                # only adds the annotation when it is absent, so declaring any policy of
                # your own opts the namespace out of the default entirely.
                "goldilocks.fairwinds.com/vpa-resource-policy": (
                    '{ "containerPolicies": [ { "containerName": "*", "controlledValues":'
                    ' "RequestsOnly", "minAllowed": { "cpu": "100m", "memory": "128Mi" } } ] }\n'
                )
            },
        ),
    )
    _session_secret(chart)
    k8s.KubeServiceAccount(chart, "serviceaccount", metadata=k8s.ObjectMeta(name=NAME, namespace=NAME))
    k8s.KubeRole(
        chart,
        "secret-writer-role",
        metadata=k8s.ObjectMeta(name=_SECRET_WRITER, namespace=NAME),
        rules=[
            k8s.PolicyRule(
                api_groups=[""], resources=["secrets"], verbs=["get", "list", "create", "update", "patch", "delete"]
            )
        ],
    )
    k8s.KubeRoleBinding(
        chart,
        "secret-writer-rolebinding",
        metadata=k8s.ObjectMeta(name=_SECRET_WRITER, namespace=NAME),
        role_ref=k8s.RoleRef(api_group="rbac.authorization.k8s.io", kind="Role", name=_SECRET_WRITER),
        subjects=[k8s.Subject(kind="ServiceAccount", name=NAME, namespace=NAME)],
    )
    k8s.KubeService(
        chart,
        "service",
        metadata=k8s.ObjectMeta(name=NAME, namespace=NAME, labels=_LABELS),
        spec=k8s.ServiceSpec(
            selector=_LABELS,
            ports=[
                k8s.ServicePort(name="http", port=_PORT, target_port=k8s.IntOrString.from_number(_PORT), protocol="TCP")
            ],
            type="ClusterIP",
        ),
    )
    # Traffic flows directly: Gateway → backend. The backend handles OIDC and protects browser
    # APIs with its same-origin session cookie.
    https_route(
        chart,
        "httproute",
        metadata=metadata(NAME, NAME),
        hostname="airlock.allegedly.works",
        backend=NAME,
        port=_PORT,
        timeout="120s",
        hsts=False,
        listener=None,
    )
    # Airlock serves only its browser OAuth broker over port 8765.
    cilium.network_policy(
        chart,
        "ciliumnetworkpolicy",
        metadata=metadata("airlock-ingress", NAME),
        selector=_LABELS,
        ingress=[
            _ingress_from(CiliumNetworkPolicySpecIngressFromEntities.INGRESS),
            # Kubelet liveness/readiness probes originate from the node host.
            _ingress_from(CiliumNetworkPolicySpecIngressFromEntities.HOST),
        ],
    )
    # google-access-token carries read-only Google scopes. Never mirrored into a namespace an agent
    # can read Secrets in (docs/personal_agents/verdicts.md): agents reach Google through a proxy
    # that presents the token for them.
    _mirror(
        chart,
        "google-access-token",
        # agentplane-staging's egress proxy substitutes this into a sandbox's Google read requests
        # (cluster/cdk8s/agentplane/egress_staging_credentials.py's `google-readonly`
        # EgressCredential). The proxy's Secret watch is scoped to its isolated credentials
        # namespace (cluster/cdk8s/agentplane/egress_credentials.py's STAGING_NAMESPACE), not the
        # app namespace. Not agentplane-testing: testing reaches no real account.
        ["agentplane-staging-egress-credentials"],
    )
    # google-write-access-token (write-scoped Gmail/Calendar) goes into google-mcp only -- the
    # standalone MCP server that fronts it behind agentplane's approval-gated ActionGroups
    # (x/google_mcp_server, cluster/cdk8s/google_mcp.py). Deliberately not mirrored into
    # claude-sandbox, haku-sandbox, or agentplane-staging directly: unlike google-access-token,
    # which an egress proxy may present on a sandbox's reads, this token can send mail and mutate
    # Gmail/Calendar, so it must never reach a namespace the agent's own execution can read from
    # directly. See plans/personal_agents/personal_data_agent.md and docs/personal_agents/verdicts.md.
    _mirror(chart, "google-write-access-token", ["google-mcp"])
    return chart


def write_manifests(root: Path) -> None:
    write_charts(root, OUTPUT_DIR, chart)
