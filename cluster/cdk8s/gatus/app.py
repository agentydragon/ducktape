"""Gatus: the Helm release, its Postgres, network policies, route and ServiceMonitor.

Hand-written beside the generated output: `config.yaml` (Gatus's own config, rendered into
the `gatus-config` ConfigMap by the directory's `configMapGenerator`) and the
`kustomization.yaml` that generates it.
"""

from __future__ import annotations

from pathlib import Path

from cdk8s import ApiObjectMetadata, App, Chart
from cdk8s_plus_34 import k8s
from cilium_crds.io.cilium import CiliumNetworkPolicySpecEgress, CiliumNetworkPolicySpecEgressToEntities
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
from prometheus_operator_crds.com.coreos.monitoring import ServiceMonitorSpecSelector

from cluster.cdk8s import cilium, cnpg, namespaces, node_scheduling
from cluster.cdk8s.gateway import https_route
from cluster.cdk8s.generation import write_charts
from cluster.cdk8s.helm import helm_release
from cluster.cdk8s.manifest_roots import HAND_WRITTEN_ROOT
from cluster.cdk8s.namespaces import AgentReadable, Vpa
from cluster.cdk8s.providers.cilium.network_policy import IngressRule, NetworkPolicy
from cluster.cdk8s.providers.prometheus_operator.service_monitor import Endpoint, ServiceMonitor

OUTPUT_DIR = f"{HAND_WRITTEN_ROOT}/gatus"
_NAME = "gatus"
_NAMESPACE = "gatus"
_LABELS = {"app.kubernetes.io/name": _NAME}
_DB_NAME = "gatus-db"
_HELM_REPOSITORY = "twin"
_PORT = 8080


def _namespace(scope: Construct) -> None:
    namespaces.namespace(scope, "namespace", name=_NAMESPACE, vpa=Vpa.AUTO, agent_readable=AgentReadable.LOGS)


def _database(scope: Construct) -> None:
    cnpg.cluster(
        scope,
        "database",
        name=_DB_NAME,
        namespace=_NAMESPACE,
        node_selector=node_scheduling.HIL_OVH_NODE_SELECTOR,
        storage_class="local-path-ovh",
        size="1Gi",
        # CNPG auto-generates credentials in secret gatus-db-app
        initdb=cnpg.same_owner_initdb("gatus"),
    )


def _helm_release(scope: Construct) -> None:
    repository = HelmRepository(
        scope,
        "helm-repository",
        metadata=ApiObjectMetadata(name=_HELM_REPOSITORY, namespace=_NAMESPACE),
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
            "ingress": {"enabled": False},
            # Storage moved off the local SQLite PVC onto the gatus-db CNPG
            # cluster on OVH-HA (the Cluster above).
            "persistence": {"enabled": False},
            # The ServiceMonitor is its own object below, to avoid blocking
            # Gatus deploys on monitoring-stack readiness.
            "serviceMonitor": {"enabled": False},
            "nodeSelector": node_scheduling.HIL_OVH_NODE_SELECTOR,
            # Gatus is stateless at the pod level (state moved to gatus-db above). Allow
            # control-plane nodes as overflow capacity, while the affinity below keeps
            # ordinary placement on workers.
            "tolerations": [node_scheduling.CONTROL_PLANE_TOLERATION],
            "affinity": node_scheduling.PREFER_WORKERS,
            "resources": {"requests": {"cpu": "20m", "memory": "64Mi"}, "limits": {"cpu": "200m", "memory": "128Mi"}},
        },
    )


def _network_policies(scope: Construct) -> None:
    # Restrict Gatus ingress to the Cilium gateway and Prometheus scraping. With native
    # OIDC, auth is handled by Gatus itself — gateway passes traffic directly.
    #
    # Uses CiliumNetworkPolicy because standard K8s NetworkPolicy cannot match Cilium
    # Gateway API traffic (reserved:ingress identity via hostNetwork Envoy).
    NetworkPolicy(
        scope,
        "ingress",
        metadata=ApiObjectMetadata(name="gatus-ingress", namespace=_NAMESPACE),
        endpoint_selector=_LABELS,
        ingress=[
            # Cilium Gateway API (reserved:ingress identity) → Gatus
            IngressRule.from_gateway(_PORT),
            # Prometheus → Gatus (ServiceMonitor scraping)
            IngressRule.from_endpoints({"k8s:io.kubernetes.pod.namespace": "monitoring"}, ports=[_PORT]),
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
    NetworkPolicy(
        scope,
        "dns-visibility",
        metadata=ApiObjectMetadata(name="gatus-dns-visibility", namespace=_NAMESPACE),
        endpoint_selector=_LABELS,
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
        metadata=ApiObjectMetadata(name=_NAME, namespace=_NAMESPACE),
        hostnames=["status.allegedly.works"],
        backend=_NAME,
        port=80,
        hsts=False,
        listener=None,
    )
    ServiceMonitor(
        chart,
        "service-monitor",
        metadata=ApiObjectMetadata(name=_NAME, namespace=_NAMESPACE),
        selector=ServiceMonitorSpecSelector(match_labels=_LABELS),
        endpoints=[Endpoint.plain(port="http")],
    )
    return chart


def write_manifests(root: Path) -> None:
    write_charts(root, OUTPUT_DIR, chart)
