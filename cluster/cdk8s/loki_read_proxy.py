"""The Loki read proxy (cluster/k8s/agents/loki-read-proxy): a read-only,
namespace-filtering Loki query proxy for agents, its Service, pull credentials and the
CiliumNetworkPolicy that is the whole access control for anonymous requests. The proxy's
source and image build live in //cluster/proxies/loki_read_proxy.

The image tag is the placeholder "unset"; the hand-written `image-pins/kustomization.yaml`
beside the output overrides it via Flux's image-automation marker.
"""

from __future__ import annotations

from pathlib import Path

from cdk8s import App, Chart
from cdk8s_plus_34 import k8s
from flux_kustomize.io.fluxcd.toolkit.kustomize import KustomizationSpecHealthChecks
from source_watcher_crds.io.fluxcd.extensions.source import ArtifactGeneratorSpecArtifacts

from cluster.cdk8s import cilium
from cluster.cdk8s.flux import (
    Kustomization,
    flux_kustomization,
    flux_kustomization_depends_on_many,
    kustomize_kustomization,
)
from cluster.cdk8s.forgejo_images import SECRET_NAME, forgejo_images_creds_external_secret
from cluster.cdk8s.generation import write_charts, write_yaml
from cluster.cdk8s.metadata import metadata

NAME = "loki-read-proxy"
OUTPUT_DIR = "cluster/k8s/agents/loki-read-proxy"
_LABELS = {"app.kubernetes.io/name": NAME}
_IMAGE = "git.allegedly.works/ducktape-ci/loki-read-proxy:unset"
_PORT = 8080

# Namespaces an *anonymous* request may query. A request carrying a Kubernetes bearer token
# ignores this list: its namespaces are authorized by the token's RBAC instead.
# cluster/validation:test_cluster_integration asserts every Namespace with
# rbac.ducktape.io/agent-readable-logs=true is included here, so the GitOps label and Loki
# policy cannot drift. csi-proxmox and cpap-sync are separately reviewed Haku-only
# exceptions retained from the original proxy policy.
NAMESPACE_ALLOWLIST = (
    "activitywatch",
    "agentplane-index",
    "agentplane-staging",
    "agentplane-testing",
    "airlock",
    "analytics",
    "authentik",
    "cert-manager",
    "clickhouse",
    "cli-proxy-api",
    "cnpg-system",
    "cpap-sync",
    "csi-proxmox",
    "docker-ci",
    "flux-system",
    "gatus",
    "grocy-sf",
    "grocy-vallejo",
    "haku-ci",
    "kube-system",
    "litellm",
    "local-path-storage",
    "loki",
    "monitoring",
    "node-feature-discovery",
    "nvidia-device-plugin",
    "oci-cache",
    "openebs",
    "plaid-mcp",
    "proxmox-proxy",
    "props",
    "study-casino",
    "tana-mcp",
)


def _healthz(*, initial_delay_seconds: int, period_seconds: int) -> k8s.Probe:
    return k8s.Probe(
        http_get=k8s.HttpGetAction(path="/healthz", port=k8s.IntOrString.from_string("http")),
        initial_delay_seconds=initial_delay_seconds,
        period_seconds=period_seconds,
    )


def _deployment(chart: Chart) -> None:
    k8s.KubeDeployment(
        chart,
        "deployment",
        metadata=k8s.ObjectMeta(
            name=NAME,
            namespace=NAME,
            labels=_LABELS,
            annotations={
                "description": (
                    "Read-only namespace-filtering Loki query proxy for agents. Exposes only GET"
                    " query/query_range and requires an exact namespace matcher in every query; Loki"
                    " itself is auth_enabled:false, so agents must never reach it directly. A request"
                    " with a Kubernetes bearer token is authorized by that token's RBAC"
                    " (SelfSubjectAccessReview for get pods/log in the namespace); an anonymous request"
                    " by the static NAMESPACE_ALLOWLIST, fenced by the CiliumNetworkPolicy next to it."
                )
            },
        ),
        spec=k8s.DeploymentSpec(
            replicas=1,
            selector=k8s.LabelSelector(match_labels=_LABELS),
            template=k8s.PodTemplateSpec(
                metadata=k8s.ObjectMeta(labels=_LABELS),
                spec=k8s.PodSpec(
                    image_pull_secrets=[k8s.LocalObjectReference(name=SECRET_NAME)],
                    automount_service_account_token=False,
                    security_context=k8s.PodSecurityContext(
                        run_as_non_root=True,
                        run_as_user=1000,
                        run_as_group=1000,
                        fs_group=1000,
                        seccomp_profile=k8s.SeccompProfile(type="RuntimeDefault"),
                    ),
                    containers=[
                        k8s.Container(
                            name="proxy",
                            image=_IMAGE,
                            image_pull_policy="Always",
                            security_context=k8s.SecurityContext(
                                allow_privilege_escalation=False, capabilities=k8s.Capabilities(drop=["ALL"])
                            ),
                            ports=[k8s.ContainerPort(name="http", container_port=_PORT, protocol="TCP")],
                            env=[
                                k8s.EnvVar(name="NAMESPACE_ALLOWLIST", value=",".join(NAMESPACE_ALLOWLIST)),
                                # Points at loki-read directly, bypassing loki-gateway's nginx —
                                # matches how Grafana's own datasource reaches Loki. nginx's DNS
                                # resolver can get permanently pinned to a dead CoreDNS pod IP
                                # after CoreDNS reschedules (ducktape#4750); this proxy only
                                # ever queries, so it never needed gateway's read/write path
                                # routing in the first place. Must stay in sync with the egress
                                # rule below, which only allows loki-read.
                                k8s.EnvVar(name="UPSTREAM_URL", value="http://loki-read.loki.svc:3100"),
                                # The pod mounts no ServiceAccount token (bearer requests are
                                # authorized with the caller's own token), so the apiserver CA
                                # comes from the kube-root-ca.crt ConfigMap volume below.
                                k8s.EnvVar(name="KUBERNETES_CA_FILE", value="/var/run/kube-root-ca/ca.crt"),
                            ],
                            volume_mounts=[
                                k8s.VolumeMount(name="kube-root-ca", mount_path="/var/run/kube-root-ca", read_only=True)
                            ],
                            resources=k8s.ResourceRequirements(
                                requests={
                                    "cpu": k8s.Quantity.from_string("50m"),
                                    "memory": k8s.Quantity.from_string("64Mi"),
                                },
                                limits={
                                    "cpu": k8s.Quantity.from_string("200m"),
                                    "memory": k8s.Quantity.from_string("256Mi"),
                                },
                            ),
                            readiness_probe=_healthz(initial_delay_seconds=3, period_seconds=10),
                            liveness_probe=_healthz(initial_delay_seconds=10, period_seconds=20),
                        )
                    ],
                    volumes=[
                        k8s.Volume(name="kube-root-ca", config_map=k8s.ConfigMapVolumeSource(name="kube-root-ca.crt"))
                    ],
                ),
            ),
        ),
    )


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    forgejo_images_creds_external_secret(chart, "forgejo-images-creds", namespace=NAME)
    k8s.KubeNamespace(
        chart, "namespace", metadata=k8s.ObjectMeta(name=NAME, labels={"goldilocks.fairwinds.com/enabled": "false"})
    )
    _deployment(chart)
    k8s.KubeService(
        chart,
        "service",
        metadata=k8s.ObjectMeta(name=NAME, namespace=NAME, labels=_LABELS),
        spec=k8s.ServiceSpec(
            selector=_LABELS,
            ports=[
                k8s.ServicePort(name="http", port=80, target_port=k8s.IntOrString.from_string("http"), protocol="TCP")
            ],
            type="ClusterIP",
        ),
    )
    # Who may reach the proxy at all. Anonymous requests are authorized only by the proxy's
    # static allowlist, so this ingress fence is their access control; token-bearing requests
    # are additionally authorized by the caller's Kubernetes RBAC. Egress is restricted too, so
    # a compromised proxy cannot pivot anywhere else either.
    cilium.network_policy(
        chart,
        "cilium-network-policy",
        metadata=metadata(
            NAME,
            NAME,
            annotations={
                "description": (
                    "Ingress to the Loki read proxy only from haku-sandbox pods; egress only to loki-read,"
                    " kube-apiserver (SelfSubjectAccessReview for bearer requests) and kube-dns. This policy"
                    " is the whole access control for anonymous (allowlist) requests."
                )
            },
        ),
        selector=_LABELS,
        # Haku agent sandboxes → proxy (namespace-filtered log reads)
        ingress=[cilium.ingress_from({"k8s:io.kubernetes.pod.namespace": "haku-sandbox"}, ports=[_PORT])],
        egress=[
            # kube-dns (resolve loki-read.loki.svc)
            cilium.dns_egress(),
            # loki-read (UPSTREAM_URL) — bypasses loki-gateway's nginx entirely (ducktape#4750).
            cilium.egress_to(
                {
                    "app.kubernetes.io/name": "loki",
                    "app.kubernetes.io/component": "read",
                    "k8s:io.kubernetes.pod.namespace": "loki",
                },
                3100,
            ),
            # kube-apiserver: SelfSubjectAccessReview with the caller's bearer token.
            cilium.egress_to_entities("kube-apiserver", ports=[443, 6443]),
        ],
    )
    return chart


def write_manifests(root: Path) -> None:
    write_charts(root, OUTPUT_DIR, chart)
    write_yaml(
        root / OUTPUT_DIR / "kustomization.yaml",
        kustomize_kustomization(resources=[f"{NAME}.k8s.yaml"], components=["./image-pins"]),
    )


def loki_read_proxy(
    chart: Chart, artifact: ArtifactGeneratorSpecArtifacts, external_secrets_operator: Kustomization
) -> Kustomization:
    return flux_kustomization(
        chart,
        NAME,
        artifact,
        timeout="5m",
        depends_on=flux_kustomization_depends_on_many(external_secrets_operator),
        health_checks=[
            KustomizationSpecHealthChecks(api_version="apps/v1", kind="Deployment", name=NAME, namespace=NAME)
        ],
        description=(
            "Read-only namespace-filtering Loki query proxy so Haku can read logs "
            "for allowlisted namespaces without touching Loki "
            "(auth_enabled:false) directly."
        ),
    )
