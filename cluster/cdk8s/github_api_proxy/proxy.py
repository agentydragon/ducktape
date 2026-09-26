"""The central GitHub API proxy: its Namespace and Certificates (`identity/`), and the proxy
workload with its capture storage, Service, network policy, dedicated TLS-passthrough Gateway
and TLSRoute, PodMonitor and alert PrometheusRule (`app/`).

The proxy image tag is the placeholder "unset"; the hand-written
cluster/k8s/github-api-proxy/app/image-pins/kustomization.yaml overrides it at `kustomize build`
time via Flux's image-automation marker. Also hand-written in `app/`: the client credential
SOPS Secrets, `config.json` (rendered by the `configMapGenerator`) and that
`kustomization.yaml`. The Flux Kustomization substitutes `${LETSENCRYPT_ISSUER}`
(`cert-manager-issuer-config`) into the server Certificate.
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

from cdk8s import App, Chart
from cdk8s_plus_34 import k8s
from cert_manager_crds.io.cert_manager import (
    Certificate,
    CertificateSpec,
    CertificateSpecIssuerRef,
    CertificateSpecPrivateKey,
    CertificateSpecPrivateKeyAlgorithm,
    CertificateSpecPrivateKeyRotationPolicy,
    CertificateSpecUsages,
)
from cilium_crds.io.cilium import (
    CiliumNetworkPolicySpecEgressDeny,
    CiliumNetworkPolicySpecEgressDenyToPorts,
    CiliumNetworkPolicySpecEgressDenyToPortsPorts,
    CiliumNetworkPolicySpecEgressDenyToPortsPortsProtocol,
)
from constructs import Construct
from gateway_api_gateway_crds.io.k8s.networking.gateway import (
    Gateway,
    GatewaySpec,
    GatewaySpecListeners,
    GatewaySpecListenersAllowedRoutes,
    GatewaySpecListenersAllowedRoutesNamespaces,
    GatewaySpecListenersAllowedRoutesNamespacesFrom,
    GatewaySpecListenersTls,
    GatewaySpecListenersTlsMode,
)
from gateway_api_tlsroute_crds.io.k8s.networking.gateway import (
    TlsRoute,
    TlsRouteSpec,
    TlsRouteSpecParentRefs,
    TlsRouteSpecRules,
    TlsRouteSpecRulesBackendRefs,
)
from prometheus_operator_podmonitor_crds.com.coreos.monitoring import (
    PodMonitor,
    PodMonitorSpec,
    PodMonitorSpecPodMetricsEndpoints,
    PodMonitorSpecSelector,
)
from prometheus_operator_prometheusrule_crds.com.coreos.monitoring import (
    PrometheusRule,
    PrometheusRuleSpec,
    PrometheusRuleSpecGroups,
    PrometheusRuleSpecGroupsRules,
    PrometheusRuleSpecGroupsRulesExpr,
)

from cluster.cdk8s import cilium
from cluster.cdk8s.forgejo_images import SECRET_NAME, forgejo_images_creds_external_secret
from cluster.cdk8s.generation import write_charts
from cluster.cdk8s.manifest_roots import HAND_WRITTEN_ROOT
from cluster.cdk8s.metadata import metadata
from cluster.cdk8s.providers.cilium.network_policy import EgressRule, Entity, IngressRule, NetworkPolicy

_IDENTITY_DIR = f"{HAND_WRITTEN_ROOT}/github-api-proxy/identity"
_APP_DIR = f"{HAND_WRITTEN_ROOT}/github-api-proxy/app"
_NAME = "github-api-proxy"
_NAMESPACE = "github-api-proxy"
_LABELS = {"app.kubernetes.io/name": _NAME}
_IMAGE = "git.allegedly.works/ducktape-ci/github-api-proxy:unset"
_HOSTNAME = "github-proxy.allegedly.works"
_SERVER_TLS_SECRET = "github-api-proxy-server-tls"
_INTERCEPTION_CA = "github-api-proxy-interception-ca"
_CAPTURE_CLAIM = "github-api-proxy-capture"
_PROXY_PORT = 8080
_METRICS_PORT = 9090
_RUN_DIR = "/run/github-api-proxy"
_CLIENTS = ("wyrm2", "rugged")
_TLS_LISTENER = "proxy-tls"

_RULES = [
    PrometheusRuleSpecGroupsRules(
        alert="GitHubProxyCaptureWriteFailed",
        expr=PrometheusRuleSpecGroupsRulesExpr.from_string(
            'max by (channel) (github_api_proxy_capture_write_failures_total{namespace="github-api-proxy"}) > 0'
        ),
        labels={"severity": "warning"},
        annotations={
            "summary": "Central GitHub proxy capture has lost {{ $labels.channel }} observations",
            "description": (
                "A private capture append failed. Readiness stays false until a controlled restart, but existing "
                "connections may continue and the quota mitigation remains active. Inspect storage and preserve the "
                "incomplete evidence before restarting; do not count this interval as complete observation coverage.\n"
            ),
        },
    ),
    PrometheusRuleSpecGroupsRules(
        alert="GitHubProxyMetricsScrapeFailed",
        expr=PrometheusRuleSpecGroupsRulesExpr.from_string(
            'max(up{namespace="github-api-proxy",job="github-api-proxy/github-api-proxy"}) == 0'
        ),
        for_="2m",
        labels={"severity": "warning"},
        annotations={
            "summary": "Central GitHub proxy metrics cannot be scraped",
            "description": (
                "All discovered proxy metrics targets have failed for two minutes. Check the Pod, private metrics "
                "listener and Alloy network path.\n"
            ),
        },
    ),
    PrometheusRuleSpecGroupsRules(
        alert="GitHubProxyMetricsMissing",
        expr=PrometheusRuleSpecGroupsRulesExpr.from_string(
            'absent_over_time(up{namespace="github-api-proxy",job="github-api-proxy/github-api-proxy"}[5m])'
        ),
        labels={"severity": "warning"},
        annotations={
            "summary": "Central GitHub proxy observations are missing",
            "description": (
                "No proxy scrape result has been retained for five minutes. Check Pod discovery, the PodMonitor, Alloy "
                "collection and remote write. Missing telemetry cannot establish a healthy proxy or quiet quota.\n"
            ),
        },
    ),
    PrometheusRuleSpecGroupsRules(
        record="github_api_proxy:capture_collection_physical_storage_budget:ratio",
        expr=PrometheusRuleSpecGroupsRulesExpr.from_string(
            dedent(
                """\
                sum by (namespace, persistentvolumeclaim) (
                  label_replace(
                    sum by (collection) (
                      max by (collection, instance) (
                        SeaweedFS_volumeServer_total_disk_size{namespace="seaweedfs",type="normal"}
                      )
                    ),
                    "volumename", "$1", "collection", "(.+)"
                  )
                  * on (volumename) group_left(namespace, persistentvolumeclaim)
                    max by (volumename, namespace, persistentvolumeclaim) (
                      kube_persistentvolumeclaim_info{
                        namespace="github-api-proxy",persistentvolumeclaim="github-api-proxy-capture",
                        storageclass="seaweedfs-ovh"
                      }
                    )
                )
                / on (namespace, persistentvolumeclaim)
                  (
                    max by (namespace, persistentvolumeclaim) (
                      kube_persistentvolumeclaim_resource_requests_storage_bytes{
                        namespace="github-api-proxy",persistentvolumeclaim="github-api-proxy-capture"
                      }
                    ) > 0
                  )
                """
            )
        ),
    ),
    PrometheusRuleSpecGroupsRules(
        alert="GitHubProxyCaptureCollectionStorageBudgetHigh",
        expr=PrometheusRuleSpecGroupsRulesExpr.from_string(
            "github_api_proxy:capture_collection_physical_storage_budget:ratio > 0.85"
        ),
        for_="5m",
        labels={"severity": "warning"},
        annotations={
            "summary": "Central GitHub proxy collection exceeds 85% of its physical storage budget",
            "description": (
                "Reported normal-volume bytes, including replicas, exceed 85% of the PVC storage request. This is an "
                "operational budget, not free space or guaranteed write capacity. Captures append without automatic "
                "deletion. Check volume-server telemetry and arrange explicit retention or expansion; preserve "
                "investigation evidence.\n"
            ),
        },
    ),
    PrometheusRuleSpecGroupsRules(
        alert="GitHubProxyCaptureStorageBudgetInputsMissing",
        expr=PrometheusRuleSpecGroupsRulesExpr.from_string(
            dedent(
                """\
                absent(github_api_proxy:capture_collection_physical_storage_budget:ratio{
                  namespace="github-api-proxy",persistentvolumeclaim="github-api-proxy-capture"
                })
                """
            )
        ),
        for_="5m",
        labels={"severity": "warning"},
        annotations={
            "summary": "Central GitHub proxy collection storage budget cannot be observed",
            "description": (
                "The collection byte metric, PVC-to-collection mapping or positive PVC storage request is missing. Check "
                "SeaweedFS volume-server scrapes and kube-state-metrics. Absence is not zero usage. Partial volume-server "
                "loss can still undercount a present budget ratio.\n"
            ),
        },
    ),
]


def _namespace(scope: Construct) -> None:
    k8s.KubeNamespace(
        scope,
        "namespace",
        metadata=k8s.ObjectMeta(
            name=_NAMESPACE,
            labels={
                "goldilocks.fairwinds.com/enabled": "true",
                "goldilocks.fairwinds.com/vpa-update-mode": "off",
                "pod-security.kubernetes.io/enforce": "restricted",
                "pod-security.kubernetes.io/enforce-version": "latest",
                "pod-security.kubernetes.io/audit": "restricted",
                "pod-security.kubernetes.io/warn": "restricted",
            },
        ),
    )


def _certificates(scope: Construct) -> None:
    Certificate(
        scope,
        "server",
        metadata=metadata("github-api-proxy-server", _NAMESPACE),
        spec=CertificateSpec(
            secret_name=_SERVER_TLS_SECRET,
            dns_names=[_HOSTNAME],
            private_key=CertificateSpecPrivateKey(
                algorithm=CertificateSpecPrivateKeyAlgorithm.ECDSA,
                size=256,
                rotation_policy=CertificateSpecPrivateKeyRotationPolicy.ALWAYS,
            ),
            usages=[CertificateSpecUsages.SERVER_AUTH],
            issuer_ref=CertificateSpecIssuerRef(name="${LETSENCRYPT_ISSUER}", kind="ClusterIssuer"),
        ),
    )
    Certificate(
        scope,
        "interception-ca",
        metadata=metadata(
            _INTERCEPTION_CA,
            _NAMESPACE,
            annotations={
                "description": (
                    "Dedicated workstation proxy interception root. Only its public certificate may be "
                    "distributed to clients; the signing key stays in this namespace."
                )
            },
        ),
        spec=CertificateSpec(
            is_ca=True,
            common_name="ducktape-github-api-proxy-interception-ca",
            secret_name=_INTERCEPTION_CA,
            duration="87600h",
            renew_before="8760h",
            private_key=CertificateSpecPrivateKey(
                algorithm=CertificateSpecPrivateKeyAlgorithm.ECDSA,
                size=256,
                # A signing-key rotation requires an explicit client trust migration.
                rotation_policy=CertificateSpecPrivateKeyRotationPolicy.NEVER,
            ),
            usages=[CertificateSpecUsages.CERT_SIGN, CertificateSpecUsages.CRL_SIGN],
            issuer_ref=CertificateSpecIssuerRef(name="cluster-ca-bootstrap", kind="ClusterIssuer"),
        ),
    )


def identity_chart(app: App) -> Chart:
    chart = Chart(app, f"{_NAME}-identity", disable_resource_name_hashes=True)
    _namespace(chart)
    _certificates(chart)
    return chart


def _capture_claim(scope: Construct) -> None:
    k8s.KubePersistentVolumeClaim(
        scope,
        "capture",
        metadata=k8s.ObjectMeta(
            name=_CAPTURE_CLAIM,
            namespace=_NAMESPACE,
            annotations={
                "kustomize.toolkit.fluxcd.io/prune": "disabled",
                "description": (
                    "Private investigation evidence. Retain on app removal; deletion requires explicit "
                    "evidence-retention review."
                ),
            },
        ),
        spec=k8s.PersistentVolumeClaimSpec(
            access_modes=["ReadWriteMany"],
            storage_class_name="seaweedfs-ovh",
            resources=k8s.VolumeResourceRequirements(requests={"storage": k8s.Quantity.from_string("100Gi")}),
        ),
    )


def _proxy_container() -> k8s.Container:
    return k8s.Container(
        name="proxy",
        image=_IMAGE,
        image_pull_policy="IfNotPresent",
        args=["--config", f"{_RUN_DIR}/config/config.json"],
        ports=[
            k8s.ContainerPort(name="proxy", container_port=_PROXY_PORT, protocol="TCP"),
            k8s.ContainerPort(name="metrics", container_port=_METRICS_PORT, protocol="TCP"),
        ],
        security_context=k8s.SecurityContext(
            allow_privilege_escalation=False, capabilities=k8s.Capabilities(drop=["ALL"])
        ),
        resources=k8s.ResourceRequirements(
            requests={"cpu": k8s.Quantity.from_string("100m"), "memory": k8s.Quantity.from_string("256Mi")},
            limits={"cpu": k8s.Quantity.from_string("2"), "memory": k8s.Quantity.from_string("2Gi")},
        ),
        readiness_probe=k8s.Probe(
            http_get=k8s.HttpGetAction(path="/healthz", port=k8s.IntOrString.from_string("metrics")),
            initial_delay_seconds=3,
            period_seconds=5,
            timeout_seconds=2,
        ),
        volume_mounts=[
            k8s.VolumeMount(name="config", mount_path=f"{_RUN_DIR}/config", read_only=True),
            k8s.VolumeMount(name="clients", mount_path=f"{_RUN_DIR}/clients", read_only=True),
            k8s.VolumeMount(name="outer-tls", mount_path=f"{_RUN_DIR}/outer-tls", read_only=True),
            k8s.VolumeMount(name="interception-ca", mount_path=f"{_RUN_DIR}/interception-ca", read_only=True),
            k8s.VolumeMount(name="private-work", mount_path=f"{_RUN_DIR}/work"),
            k8s.VolumeMount(name="capture", mount_path="/var/lib/github-api-proxy"),
        ],
    )


def _deployment(scope: Construct) -> None:
    k8s.KubeDeployment(
        scope,
        "deployment",
        metadata=k8s.ObjectMeta(
            name=_NAME, namespace=_NAMESPACE, labels=_LABELS, annotations={"reloader.stakater.com/auto": "true"}
        ),
        spec=k8s.DeploymentSpec(
            replicas=1,
            strategy=k8s.DeploymentStrategy(type="Recreate"),
            selector=k8s.LabelSelector(match_labels=_LABELS),
            template=k8s.PodTemplateSpec(
                metadata=k8s.ObjectMeta(labels=_LABELS),
                spec=k8s.PodSpec(
                    # No explicit zone nodeSelector: github-api-proxy-capture's seaweedfs-ovh
                    # StorageClass already pins scheduling to topology.kubernetes.io/zone=hil-ovh
                    # via WaitForFirstConsumer + allowedTopologies
                    # (seaweedfs_csi/driver.py).
                    image_pull_secrets=[k8s.LocalObjectReference(name=SECRET_NAME)],
                    automount_service_account_token=False,
                    termination_grace_period_seconds=60,
                    security_context=k8s.PodSecurityContext(
                        run_as_non_root=True,
                        run_as_user=1000,
                        run_as_group=1000,
                        fs_group=1000,
                        fs_group_change_policy="OnRootMismatch",
                        seccomp_profile=k8s.SeccompProfile(type="RuntimeDefault"),
                    ),
                    containers=[_proxy_container()],
                    volumes=[
                        # Rendered from config.json by the hand-written kustomization.yaml's
                        # configMapGenerator.
                        k8s.Volume(name="config", config_map=k8s.ConfigMapVolumeSource(name="github-api-proxy-config")),
                        k8s.Volume(
                            name="clients",
                            projected=k8s.ProjectedVolumeSource(
                                default_mode=0o440,
                                sources=[
                                    k8s.VolumeProjection(
                                        secret=k8s.SecretProjection(
                                            name=f"github-api-proxy-{client}-credentials",
                                            items=[
                                                k8s.KeyToPath(key=key, path=f"{client}/{key}")
                                                for key in ("username", "password")
                                            ],
                                        )
                                    )
                                    for client in _CLIENTS
                                ],
                            ),
                        ),
                        k8s.Volume(
                            name="outer-tls",
                            secret=k8s.SecretVolumeSource(secret_name=_SERVER_TLS_SECRET, default_mode=0o440),
                        ),
                        k8s.Volume(
                            name="interception-ca",
                            secret=k8s.SecretVolumeSource(secret_name=_INTERCEPTION_CA, default_mode=0o440),
                        ),
                        k8s.Volume(
                            name="private-work",
                            empty_dir=k8s.EmptyDirVolumeSource(
                                medium="Memory", size_limit=k8s.Quantity.from_string("16Mi")
                            ),
                        ),
                        k8s.Volume(
                            name="capture",
                            persistent_volume_claim=k8s.PersistentVolumeClaimVolumeSource(claim_name=_CAPTURE_CLAIM),
                        ),
                    ],
                ),
            ),
        ),
    )


def _service(scope: Construct) -> None:
    k8s.KubeService(
        scope,
        "service",
        metadata=k8s.ObjectMeta(name=_NAME, namespace=_NAMESPACE),
        spec=k8s.ServiceSpec(
            selector=_LABELS,
            ports=[
                k8s.ServicePort(
                    name="proxy-tls", port=443, target_port=k8s.IntOrString.from_string("proxy"), protocol="TCP"
                )
            ],
        ),
    )


def _network_policy(scope: Construct) -> None:
    NetworkPolicy(
        scope,
        "network-policy",
        metadata=metadata(_NAME, _NAMESPACE),
        selector=_LABELS,
        ingress=[
            IngressRule.from_gateway(_PROXY_PORT),
            IngressRule.from_endpoints(cilium.endpoint_labels("monitoring", "alloy"), ports=[_METRICS_PORT]),
        ],
        egress=[
            cilium.dns_egress(protocols=["ANY"], resolves=["*"]),
            EgressRule.to_entities(Entity.WORLD, ports=[80, 443]),
        ],
        # These non-public ranges can be outside Cilium's cluster identity set.
        # Limit the deny to web ports so the explicit cluster-DNS exception remains.
        # Runtime destination validation also fences loopback and DNS rebinding.
        egress_deny=[
            CiliumNetworkPolicySpecEgressDeny(
                to_cidr=[
                    # keep-sorted start
                    "0.0.0.0/8",
                    "10.0.0.0/8",
                    "100.64.0.0/10",
                    "127.0.0.0/8",
                    "169.254.0.0/16",
                    "172.16.0.0/12",
                    "192.168.0.0/16",
                    "::/128",
                    "::1/128",
                    "fc00::/7",
                    "fe80::/10",
                    # keep-sorted end
                ],
                to_ports=[
                    CiliumNetworkPolicySpecEgressDenyToPorts(
                        ports=[
                            CiliumNetworkPolicySpecEgressDenyToPortsPorts(
                                port=str(port), protocol=CiliumNetworkPolicySpecEgressDenyToPortsPortsProtocol.TCP
                            )
                            for port in (80, 443)
                        ]
                    )
                ],
            )
        ],
    )


def _gateway(scope: Construct) -> None:
    Gateway(
        scope,
        "gateway",
        metadata=metadata(
            _NAME,
            _NAMESPACE,
            annotations={
                "description": (
                    "Dedicated TLS-only listener; avoids overlapping the shared wildcard HTTPS listener on port 443."
                )
            },
        ),
        spec=GatewaySpec(
            gateway_class_name="cilium",
            listeners=[
                GatewaySpecListeners(
                    name=_TLS_LISTENER,
                    hostname=_HOSTNAME,
                    port=8443,
                    protocol="TLS",
                    tls=GatewaySpecListenersTls(mode=GatewaySpecListenersTlsMode.PASSTHROUGH),
                    allowed_routes=GatewaySpecListenersAllowedRoutes(
                        namespaces=GatewaySpecListenersAllowedRoutesNamespaces(
                            from_=GatewaySpecListenersAllowedRoutesNamespacesFrom.SAME
                        )
                    ),
                )
            ],
        ),
    )
    TlsRoute(
        scope,
        "tls-route",
        metadata=metadata(_NAME, _NAMESPACE),
        spec=TlsRouteSpec(
            parent_refs=[TlsRouteSpecParentRefs(name=_NAME, section_name=_TLS_LISTENER)],
            hostnames=[_HOSTNAME],
            rules=[TlsRouteSpecRules(backend_refs=[TlsRouteSpecRulesBackendRefs(name=_NAME, port=443)])],
        ),
    )


def _monitoring(scope: Construct) -> None:
    PodMonitor(
        scope,
        "pod-monitor",
        metadata=metadata(_NAME, _NAMESPACE),
        spec=PodMonitorSpec(
            selector=PodMonitorSpecSelector(match_labels=_LABELS),
            pod_metrics_endpoints=[
                PodMonitorSpecPodMetricsEndpoints(port="metrics", path="/metrics", scrape_timeout="10s")
            ],
        ),
    )
    PrometheusRule(
        scope,
        "prometheus-rule",
        metadata=metadata(_NAME, _NAMESPACE, labels={"release": "kube-prometheus-stack"}),
        spec=PrometheusRuleSpec(groups=[PrometheusRuleSpecGroups(name=_NAME, rules=_RULES)]),
    )


def app_chart(app: App) -> Chart:
    chart = Chart(app, _NAME, disable_resource_name_hashes=True)
    forgejo_images_creds_external_secret(chart, "forgejo-images-creds", namespace=_NAMESPACE)
    _capture_claim(chart)
    _deployment(chart)
    _network_policy(chart)
    _service(chart)
    _gateway(chart)
    _monitoring(chart)
    return chart


def write_manifests(root: Path) -> None:
    write_charts(root, _IDENTITY_DIR, identity_chart)
    write_charts(root, _APP_DIR, app_chart)
