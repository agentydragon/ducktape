"""Gatus: the Helm release, its Postgres, network policies, route and ServiceMonitor.

Hand-written beside the generated output: `config.yaml` (Gatus's own config, rendered into
the `gatus-config` ConfigMap by the directory's `configMapGenerator`) and the
`kustomization.yaml` that generates it.
"""

from __future__ import annotations

from pathlib import Path

from cdk8s import App, Chart
from cdk8s_plus_34 import k8s
from cilium_crds.io.cilium import CiliumNetworkPolicySpecEgress, CiliumNetworkPolicySpecEgressToEntities
from cnpg_cluster_crds.io.cnpg.postgresql import ClusterSpecBootstrapInitdb
from constructs import Construct
from flux_helm.io.fluxcd.toolkit.helm import (
    HelmReleaseSpecInstall,
    HelmReleaseSpecInstallStrategy,
    HelmReleaseSpecInstallStrategyName,
    HelmReleaseSpecUpgrade,
    HelmReleaseSpecUpgradeStrategy,
    HelmReleaseSpecUpgradeStrategyName,
)
from flux_source.io.fluxcd.toolkit.source import HelmRepository, HelmRepositorySpec
from prometheus_operator_crds.com.coreos.monitoring import (
    ServiceMonitor,
    ServiceMonitorSpec,
    ServiceMonitorSpecEndpoints,
    ServiceMonitorSpecSelector,
)

from cluster.cdk8s import cilium, cnpg
from cluster.cdk8s.gateway import https_route
from cluster.cdk8s.generation import write_charts
from cluster.cdk8s.helm import helm_release
from cluster.cdk8s.metadata import metadata

_OUTPUT_DIR = "cluster/k8s/gatus"
_NAME = "gatus"
_NAMESPACE = "gatus"
_LABELS = {"app.kubernetes.io/name": _NAME}
_DB_NAME = "gatus-db"
_ZONE = "hil-ovh"
_HELM_REPOSITORY = "twin"
_PORT = 8080


def _namespace(scope: Construct) -> None:
    k8s.KubeNamespace(
        scope,
        "namespace",
        metadata=k8s.ObjectMeta(
            name=_NAMESPACE,
            labels={
                "goldilocks.fairwinds.com/enabled": "true",
                "goldilocks.fairwinds.com/vpa-update-mode": "auto",
                "rbac.ducktape.io/agent-readable-logs": "true",
            },
        ),
    )


def _database(scope: Construct) -> None:
    cnpg.cluster(
        scope,
        "database",
        name=_DB_NAME,
        namespace=_NAMESPACE,
        affinity=cnpg.affinity(node_selector={"topology.kubernetes.io/zone": _ZONE}, tolerate_control_plane=False),
        storage_class="local-path-ovh",
        size="1Gi",
        # CNPG auto-generates credentials in secret gatus-db-app
        initdb=ClusterSpecBootstrapInitdb(database="gatus", owner="gatus"),
    )


def _helm_release(scope: Construct) -> None:
    repository = HelmRepository(
        scope,
        "helm-repository",
        metadata=metadata(_HELM_REPOSITORY, _NAMESPACE),
        spec=HelmRepositorySpec(interval="24h", url="https://twin.github.io/helm-charts"),
    )
    # Empty ConfigMap required by the gatus Helm chart. The chart hardcodes
    # envFrom.configMapRef with the release name but skips creating it when
    # externalConfigMap is set (chart bug).
    k8s.KubeConfigMap(scope, "env", metadata=k8s.ObjectMeta(name=_NAME, namespace=_NAMESPACE))
    helm_release(
        scope,
        _NAME,
        _NAMESPACE,
        repository=repository,
        chart="gatus",
        version="1.5.0",
        interval="15m",
        install=HelmReleaseSpecInstall(
            strategy=HelmReleaseSpecInstallStrategy(name=HelmReleaseSpecInstallStrategyName.RETRY_ON_FAILURE)
        ),
        upgrade=HelmReleaseSpecUpgrade(
            strategy=HelmReleaseSpecUpgradeStrategy(name=HelmReleaseSpecUpgradeStrategyName.RETRY_ON_FAILURE)
        ),
        values={
            "externalConfigMap": "gatus-config",
            "env": {
                "GATUS_DB_URI": {"valueFrom": {"secretKeyRef": {"name": f"{_DB_NAME}-app", "key": "uri"}}},
                "LITELLM_API_KEY": {"valueFrom": {"secretKeyRef": {"name": "litellm-master-key", "key": "api-key"}}},
            },
            "envFrom": [{"secretRef": {"name": "gatus-oidc-secret"}}],
            "podAnnotations": {"reloader.stakater.com/auto": "true"},
            "ingress": {"enabled": False},
            # Storage moved off the local SQLite PVC onto the gatus-db CNPG
            # cluster on OVH-HA (the Cluster above).
            "persistence": {"enabled": False},
            # The ServiceMonitor is its own object below, to avoid blocking
            # Gatus deploys on monitoring-stack readiness.
            "serviceMonitor": {"enabled": False},
            "nodeSelector": {"topology.kubernetes.io/zone": _ZONE},
            # Gatus is stateless at the pod level (state moved to gatus-db above). Allow
            # control-plane nodes as overflow capacity, while the affinity below keeps
            # ordinary placement on workers.
            "tolerations": [
                {"key": "node-role.kubernetes.io/control-plane", "operator": "Exists", "effect": "NoSchedule"}
            ],
            # Prefer ordinary workers when this workload tolerates control planes.
            "affinity": {
                "nodeAffinity": {
                    "preferredDuringSchedulingIgnoredDuringExecution": [
                        {
                            "weight": 100,
                            "preference": {
                                "matchExpressions": [
                                    {"key": "node-role.kubernetes.io/control-plane", "operator": "DoesNotExist"}
                                ]
                            },
                        }
                    ]
                }
            },
            "resources": {"requests": {"cpu": "20m", "memory": "64Mi"}, "limits": {"cpu": "200m", "memory": "128Mi"}},
        },
    )


def _network_policies(scope: Construct) -> None:
    # Restrict Gatus ingress to the Cilium gateway and Prometheus scraping. With native
    # OIDC, auth is handled by Gatus itself — gateway passes traffic directly.
    #
    # Uses CiliumNetworkPolicy because standard K8s NetworkPolicy cannot match Cilium
    # Gateway API traffic (reserved:ingress identity via hostNetwork Envoy).
    cilium.network_policy(
        scope,
        "ingress",
        metadata=metadata("gatus-ingress", _NAMESPACE),
        selector=_LABELS,
        ingress=[
            # Cilium Gateway API (reserved:ingress identity) → Gatus
            cilium.ingress_from_gateway(_PORT),
            # Prometheus → Gatus (ServiceMonitor scraping)
            cilium.ingress_from({"k8s:io.kubernetes.pod.namespace": "monitoring"}, ports=[_PORT]),
        ],
    )
    # Route Gatus's DNS through Cilium's DNS proxy, so its queries are observable
    # (`hubble_dns_queries_total`) and the FQDN cache populates — which is what lets
    # `destinationContext=...|dns|...` in cilium-values.yaml report hostnames instead
    # of raw IPs.
    #
    # On Cilium 1.19 there is no observation-only mode: L7 visibility comes from a
    # policy, and a policy enforces. This one is written to enforce nothing — an
    # egress rule flips the endpoint to default-deny, so the second rule has to
    # re-admit everything Gatus reaches.
    cilium.network_policy(
        scope,
        "dns-visibility",
        metadata=metadata("gatus-dns-visibility", _NAMESPACE),
        selector=_LABELS,
        egress=[
            cilium.dns_egress(protocols=["ANY"], resolves=["*"]),
            # Everything else, deliberately unrestricted.
            #
            # No `toPorts`: egress to a ClusterIP is matched on the backend `targetPort`,
            # not the Service port, because socket-LB rewrites before policy is enforced
            # (cluster/docs/cilium_network_policy.md). Gatus probes 13 in-cluster
            # Services across as many namespaces, so enumerating their targetPorts would
            # be a list that silently rots.
            #
            # `all`, not `world`: the nine `*.allegedly.works` endpoints Gatus checks all
            # resolve to node ExternalIPs, which carry `reserved:remote-node` or
            # `reserved:host` — Cilium carves those out of `world`.
            CiliumNetworkPolicySpecEgress(to_entities=[CiliumNetworkPolicySpecEgressToEntities.ALL]),
        ],
    )


def chart(app: App) -> Chart:
    chart = Chart(app, _NAME, disable_resource_name_hashes=True)
    _namespace(chart)
    _database(chart)
    _helm_release(chart)
    _network_policies(chart)
    # Traffic flows directly: Gateway → Gatus backend. Auth is handled by Gatus's native
    # OIDC integration (no proxy outpost needed).
    https_route(
        chart,
        "route",
        metadata=metadata(_NAME, _NAMESPACE),
        hostname="status.allegedly.works",
        backend=_NAME,
        port=80,
        hsts=False,
        listener=None,
    )
    ServiceMonitor(
        chart,
        "service-monitor",
        metadata=metadata(_NAME, _NAMESPACE),
        spec=ServiceMonitorSpec(
            selector=ServiceMonitorSpecSelector(match_labels=_LABELS),
            endpoints=[ServiceMonitorSpecEndpoints(port="http", path="/metrics")],
        ),
    )
    return chart


def write_manifests(root: Path) -> None:
    write_charts(root, _OUTPUT_DIR, chart)
