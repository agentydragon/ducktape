"""Airlock's namespace, identity, session secret, Deployment, Service, route and ingress policy
(cluster/k8s/agents/airlock).

Hand-written beside the output: the SOPS client credentials; `config.yaml`, rendered into the
Deployment's ConfigMap by `kustomization.yaml`; and `image-pins/kustomization.yaml`, which pins
the image tag and copies it into `AIRLOCK_IMAGE_TAG`.
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
from external_secrets_crds.io.external_secrets import ExternalSecretSpecTargetCreationPolicy

from cluster.cdk8s import namespaces
from cluster.cdk8s.external_secrets.minted_secret import mint_bearer_secret
from cluster.cdk8s.forgejo_images import SECRET_NAME, forgejo_images_creds_external_secret
from cluster.cdk8s.gateway import https_route
from cluster.cdk8s.generation import write_charts
from cluster.cdk8s.manifest_roots import HAND_WRITTEN_ROOT
from cluster.cdk8s.namespaces import AgentReadable, Vpa
from cluster.cdk8s.providers.cilium.network_policy import NetworkPolicy
from cluster.cdk8s.providers.external_secrets.external_secret import (
    ClusterDataFrom,
    ClusterExternalSecret,
    ClusterSecretStoreRef,
)
from cluster.cdk8s.secret_ref import SecretRef
from cluster.cdk8s.service_ref import Pods, Port, ServiceRef

NAME = "airlock"
OUTPUT_DIR = f"{HAND_WRITTEN_ROOT}/agents/airlock"
SERVICE = ServiceRef(
    name=NAME,
    port=Port(name="http", number=8765),
    pods=Pods(namespace=NAME, labels=(("app.kubernetes.io/name", NAME), ("app.kubernetes.io/component", "server"))),
)
_SESSION = SecretRef(namespace=NAME, name="airlock-session-secret").key("session-secret")
_OIDC = SecretRef(namespace=NAME, name="airlock-oidc-config")
_OURA = SecretRef(namespace=NAME, name="oura-client-credentials")
_GOOGLE = SecretRef(namespace=NAME, name="google-client-credentials")
_BSC = SecretRef(namespace=NAME, name="bsc-client-credentials")
_SECRET_WRITER = "airlock-secret-writer"
# image-pins/ overrides the tag and copies it into AIRLOCK_IMAGE_TAG.
_PLACEHOLDER_TAG = "unset"
_CONFIG_MOUNT = "/etc/airlock"


def _probe(path: str, initial_delay_seconds: int, period_seconds: int) -> k8s.Probe:
    return k8s.Probe(
        http_get=k8s.HttpGetAction(path=path, port=k8s.IntOrString.from_number(SERVICE.pod_port)),
        initial_delay_seconds=initial_delay_seconds,
        period_seconds=period_seconds,
    )


def _deployment(chart: Chart) -> None:
    k8s.KubeDeployment(
        chart,
        "deployment",
        metadata=k8s.ObjectMeta(name=NAME, namespace=NAME, labels=SERVICE.pods.selector),
        spec=k8s.DeploymentSpec(
            replicas=1,
            selector=k8s.LabelSelector(match_labels=SERVICE.pods.selector),
            template=k8s.PodTemplateSpec(
                metadata=k8s.ObjectMeta(labels=SERVICE.pods.selector),
                spec=k8s.PodSpec(
                    image_pull_secrets=[k8s.LocalObjectReference(name=SECRET_NAME)],
                    service_account_name=NAME,
                    security_context=k8s.PodSecurityContext(fs_group=1000),
                    containers=[
                        k8s.Container(
                            name=NAME,
                            image=f"git.allegedly.works/ducktape-ci/airlock:{_PLACEHOLDER_TAG}",
                            image_pull_policy="Always",
                            ports=[SERVICE.port.k8s_container_port()],
                            env=[
                                k8s.EnvVar(name="AIRLOCK_IMAGE_TAG", value=_PLACEHOLDER_TAG),
                                k8s.EnvVar(name="CONFIG_PATH", value=f"{_CONFIG_MOUNT}/config.yaml"),
                                _OIDC.key("client-id").env_var("AIRLOCK_OIDC_CLIENT_ID"),
                                _OIDC.key("client-secret").env_var("AIRLOCK_OIDC_CLIENT_SECRET"),
                                _SESSION.env_var("AIRLOCK_OIDC_SESSION_SECRET"),
                                _OURA.key("client_id").env_var("OURA_CLIENT_ID"),
                                _OURA.key("client_secret").env_var("OURA_CLIENT_SECRET"),
                                _GOOGLE.key("client_id").env_var("GOOGLE_CLIENT_ID"),
                                _GOOGLE.key("client_secret").env_var("GOOGLE_CLIENT_SECRET"),
                                # The `google-write` provider reuses this same GCP OAuth client
                                # (config.yaml); scope is a per-authorize-flow parameter, not fixed to
                                # the client registration.
                                _GOOGLE.key("client_id").env_var("GOOGLE_WRITE_CLIENT_ID"),
                                _GOOGLE.key("client_secret").env_var("GOOGLE_WRITE_CLIENT_SECRET"),
                                _BSC.key("client_id").env_var("BSC_CLIENT_ID"),
                                _BSC.key("client_secret").env_var("BSC_CLIENT_SECRET"),
                            ],
                            volume_mounts=[k8s.VolumeMount(name="config", mount_path=_CONFIG_MOUNT, read_only=True)],
                            resources=k8s.ResourceRequirements(
                                requests={
                                    "memory": k8s.Quantity.from_string("256Mi"),
                                    "cpu": k8s.Quantity.from_string("100m"),
                                },
                                limits={
                                    "memory": k8s.Quantity.from_string("512Mi"),
                                    "cpu": k8s.Quantity.from_string("500m"),
                                },
                            ),
                            liveness_probe=_probe("/healthz", 15, 20),
                            readiness_probe=_probe("/healthz", 5, 10),
                        )
                    ],
                    # Generated by kustomization.yaml's configMapGenerator.
                    volumes=[k8s.Volume(name="config", config_map=k8s.ConfigMapVolumeSource(name="airlock-config"))],
                ),
            ),
        ),
    )


def _session_secret(chart: Chart) -> None:
    """Airlock's session-signing key is app-local. A key rotation invalidates existing browser
    sessions but does not need to update Authentik or another credential store."""
    mint_bearer_secret(
        chart,
        "session-secret",
        name=_SESSION.secret.name,
        namespace=_SESSION.secret.namespace,
        key=_SESSION.key,
        length=64,
        digits=16,
        creation_policy=ExternalSecretSpecTargetCreationPolicy.ORPHAN,
        immutable=True,
    )


def _mirror(chart: Chart, name: str, namespaces: list[str]) -> None:
    """Mirror the Secret `name` Airlock writes into the `airlock` namespace into `namespaces`."""
    ClusterExternalSecret(
        chart,
        name,
        metadata=ApiObjectMetadata(name=name),
        namespaces=namespaces,
        secret_store_ref=ClusterSecretStoreRef.cluster("kubernetes-airlock-secret-store"),
        refresh_interval="1m",
        data_from=[ClusterDataFrom.from_extract(name)],
    )


def _ingress_from(entity: CiliumNetworkPolicySpecIngressFromEntities) -> CiliumNetworkPolicySpecIngress:
    return CiliumNetworkPolicySpecIngress(
        from_entities=[entity],
        to_ports=[
            CiliumNetworkPolicySpecIngressToPorts(
                ports=[
                    CiliumNetworkPolicySpecIngressToPortsPorts(
                        port=str(SERVICE.pod_port), protocol=CiliumNetworkPolicySpecIngressToPortsPortsProtocol.TCP
                    )
                ]
            )
        ],
    )


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    forgejo_images_creds_external_secret(chart, "forgejo-images-creds", namespace=NAME)
    namespaces.namespace(
        chart,
        "namespace",
        name=NAME,
        vpa=Vpa.AUTO,
        agent_readable=AgentReadable.LOGS,
        labels={"name": NAME},
        annotations={
            # controlledValues has to be spelled out here: default-vpa-requests-only
            # only adds the annotation when it is absent, so declaring any policy of
            # your own opts the namespace out of the default entirely.
            "goldilocks.fairwinds.com/vpa-resource-policy": (
                '{ "containerPolicies": [ { "containerName": "*", "controlledValues":'
                ' "RequestsOnly", "minAllowed": { "cpu": "100m", "memory": "128Mi" } } ] }\n'
            )
        },
    )
    _session_secret(chart)
    _deployment(chart)
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
        metadata=k8s.ObjectMeta(name=SERVICE.name, namespace=NAME, labels=SERVICE.labels),
        spec=k8s.ServiceSpec(selector=SERVICE.pods.selector, ports=[SERVICE.port.k8s_service_port()], type="ClusterIP"),
    )
    # Traffic flows directly: Gateway → backend. The backend handles OIDC and protects browser
    # APIs with its same-origin session cookie.
    https_route(
        chart,
        "httproute",
        metadata=ApiObjectMetadata(name=NAME, namespace=NAME),
        hostnames=["airlock.allegedly.works"],
        backend=SERVICE,
        timeout="120s",
        hsts=False,
        listener=None,
    )
    # Airlock serves only its browser OAuth broker over port 8765.
    NetworkPolicy(
        chart,
        "ciliumnetworkpolicy",
        metadata=ApiObjectMetadata(name="airlock-ingress", namespace=NAME),
        endpoint_selector=SERVICE.pods.selector,
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
