"""Gatus: the Helm release, its Postgres, network policies, route and ServiceMonitor, and its
own configuration (`config.py`) in the `gatus-config` ConfigMap."""

from __future__ import annotations

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
from flux_kustomize.io.fluxcd.toolkit.kustomize import KustomizationSpecDeletionPolicy
from prometheus_operator_crds.com.coreos.monitoring import ServiceMonitorSpecSelector

from cluster.cdk8s import cilium, cnpg, namespaces, node_scheduling
from cluster.cdk8s.flux import (
    ConfigMapArgs,
    GeneratorOptions,
    Kustomization,
    RenderedDirectory,
    flux_kustomization,
    flux_kustomization_depends_on_many,
)
from cluster.cdk8s.gateway import https_route
from cluster.cdk8s.gatus import config
from cluster.cdk8s.helm import helm_release, https_helm_repository
from cluster.cdk8s.manifest_roots import GENERATED_ROOT
from cluster.cdk8s.namespaces import Vpa
from cluster.cdk8s.providers.cilium.network_policy import IngressRule, NetworkPolicy
from cluster.cdk8s.providers.prometheus_operator.service_monitor import Endpoint, ServiceMonitor
from cluster.cdk8s.secret_ref import SecretRef
from cluster.cdk8s.service_ref import Port, ServiceRef

OUTPUT_DIR = f"{GENERATED_ROOT}/gatus"
_NAME = "gatus"
_NAMESPACE = "gatus"
_HOSTNAME = "status.allegedly.works"
DATABASE = cnpg.PostgresRef.generated(name="gatus-db", namespace=_NAMESPACE)
_HELM_REPOSITORY = "twin"
# The chart's Service: port 80 to the Pods' `http` (8080), selecting `app.kubernetes.io/name`.
SERVICE = ServiceRef(name=_NAME, port=Port(name="http", number=80), pods=cilium.PROBER, target_port=8080)
_LITELLM_KEY = SecretRef(namespace=_NAMESPACE, name="litellm-master-key").key("api-key")
CONFIG_MAP = ConfigMapArgs(
    name="gatus-config",
    namespace=_NAMESPACE,
    # The Helm values name it, and kustomize cannot rewrite a reference inside a HelmRelease's values.
    options=GeneratorOptions(disable_name_suffix_hash=True),
    # The chart mounts the ConfigMap at /config; config/config.yaml is Gatus's default path.
    literals=[f"config.yaml={config.render(hostname=_HOSTNAME)}"],
)


def _namespace(scope: Construct) -> None:
    namespaces.namespace(scope, "namespace", name=_NAMESPACE, vpa=Vpa.AUTO)


def _database(scope: Construct) -> None:
    cnpg.cluster(
        scope,
        "database",
        ref=DATABASE,
        placement=node_scheduling.HIL_OVH,
        storage_class="local-path-ovh",
        size="1Gi",
        initdb=cnpg.same_owner_initdb("gatus"),
        wal_archive=False,
    )


def _helm_release(scope: Construct) -> None:
    repository = https_helm_repository(scope, _HELM_REPOSITORY, _NAMESPACE, url="https://twin.github.io/helm-charts")
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
            "externalConfigMap": CONFIG_MAP.name,
            "env": {
                config.DB_URI_ENV: {"valueFrom": DATABASE.app_secret.key("uri").value_from()},
                config.LITELLM_API_KEY_ENV: {"valueFrom": _LITELLM_KEY.value_from()},
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
            # control-plane nodes as overflow capacity.
            "tolerations": [node_scheduling.CONTROL_PLANE_TOLERATION],
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
        metadata=ApiObjectMetadata(name="ingress", namespace=_NAMESPACE),
        endpoint_selector=SERVICE.pods.selector,
        ingress=[
            # Cilium Gateway API (reserved:ingress identity) → Gatus
            IngressRule.from_gateway(SERVICE.pod_port),
            # Prometheus → Gatus (ServiceMonitor scraping)
            cilium.SCRAPERS.admit(SERVICE.pod_port),
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
        metadata=ApiObjectMetadata(name="dns-visibility", namespace=_NAMESPACE),
        endpoint_selector=SERVICE.pods.selector,
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
        hostnames=[_HOSTNAME],
        backend=SERVICE,
        hsts=False,
        listener=None,
    )
    ServiceMonitor(
        chart,
        "service-monitor",
        metadata=ApiObjectMetadata(name=_NAME, namespace=_NAMESPACE),
        selector=ServiceMonitorSpecSelector(match_labels=SERVICE.labels),
        endpoints=[Endpoint.plain(port=SERVICE.port.name)],
    )
    return chart


def gatus(
    flux_chart: Chart, directory: RenderedDirectory, cnpg: Kustomization, monitoring_crds: Kustomization
) -> Kustomization:
    return flux_kustomization(
        flux_chart,
        _NAME,
        directory,
        timeout="10m",
        deletion_policy=KustomizationSpecDeletionPolicy.ORPHAN,
        depends_on=flux_kustomization_depends_on_many(cnpg, monitoring_crds),
    )
