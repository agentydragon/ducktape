"""cert-manager: its Namespace, the jetstack HelmRepository, the HelmRelease, and the
ServiceMonitors for the controller and webhook metrics Services the chart creates.

`values` is an untyped dict: Helm values carry no schema for `cdk8s_import` to ingest.
"""

from __future__ import annotations

from cdk8s import ApiObjectMetadata, App, Chart
from cdk8s_plus_34 import k8s
from flux_source.io.fluxcd.toolkit.source import HelmRepository, HelmRepositorySpec
from prometheus_operator_crds.com.coreos.monitoring import ServiceMonitorSpecSelector

from cluster.cdk8s import node_scheduling
from cluster.cdk8s.cert_manager.config import LETSENCRYPT_ISSUER
from cluster.cdk8s.flux import Kustomization, RenderedDirectory, flux_kustomization, flux_kustomization_depends_on_many
from cluster.cdk8s.helm import RETRY_FAILED_INSTALL, helm_release
from cluster.cdk8s.manifest_roots import GENERATED_ROOT
from cluster.cdk8s.providers.prometheus_operator.service_monitor import Endpoint, ServiceMonitor

NAME = "cert-manager"
NAMESPACE = "cert-manager"
OUTPUT_DIR = f"{GENERATED_ROOT}/cert-manager/app"


def _values() -> dict[str, object]:
    return {
        # cert-manager-webhook backs a failurePolicy: Fail webhook, so while it is down
        # the API *rejects* every Certificate/Issuer/CertificateRequest write rather than
        # degrading. Without a priority class it sorted with ordinary workloads under
        # kubelet eviction, which on 2026-09-07 took out comparable infrastructure for a
        # few KiB of ephemeral storage on a node whose disk something else had filled.
        # The chart exposes priorityClassName only under `global`, so this necessarily
        # covers the controller and cainjector too — which is right: cainjector maintains
        # the caBundle that webhook depends on, and the controller is what actually
        # issues the certificates.
        "global": {"priorityClassName": "system-cluster-critical"},
        # Tolerations are per-component in this chart (no global). This one is the
        # controller; webhook and cainjector repeat it below. Small stateless components
        # in the admission path belong on the always-on control-plane bare metal, as
        # kyverno and external-secrets already are. Widened, not pinned — no nodeSelector
        # or affinity, so every worker stays a candidate.
        "tolerations": [node_scheduling.CONTROL_PLANE_TOLERATION],
        "crds": {"enabled": True},
        # Default ClusterIssuer for Ingress resources without explicit annotation.
        "ingressShim": {"defaultIssuerName": LETSENCRYPT_ISSUER, "defaultIssuerKind": "ClusterIssuer"},
        # Gateway API support — enables gateway-shim controller that watches Gateway
        # resources for cert-manager.io/cluster-issuer annotations.
        # Since v1.15 the old --feature-gates=ExperimentalGatewayAPISupport flag is
        # ignored; must use config-based enablement instead.
        "config": {
            "apiVersion": "controller.config.cert-manager.io/v1alpha1",
            "kind": "ControllerConfiguration",
            "enableGatewayAPI": True,
        },
        # Use external DNS servers for ACME challenge verification.
        # This avoids hairpin NAT issues where pods can't reach their own node's public IP.
        "extraArgs": ["--dns01-recursive-nameservers=8.8.8.8:53,1.1.1.1:53", "--dns01-recursive-nameservers-only"],
        # The ServiceMonitors are this chart's own objects, not the Helm chart's.
        "prometheus": {"enabled": True, "servicemonitor": {"enabled": False}},
        "resources": {"limits": {"cpu": "100m", "memory": "128Mi"}, "requests": {"cpu": "10m", "memory": "32Mi"}},
        "webhook": {
            "tolerations": [node_scheduling.CONTROL_PLANE_TOLERATION],
            # Additional DNS names for the webhook certificate.
            "extraArgs": [
                "--dynamic-serving-dns-names=cert-manager-webhook,cert-manager-webhook.cert-manager,"
                "cert-manager-webhook.cert-manager.svc,cert-manager-webhook.cert-manager.svc.cluster.local"
            ],
            "resources": {"limits": {"cpu": "100m", "memory": "128Mi"}, "requests": {"cpu": "10m", "memory": "32Mi"}},
        },
        "cainjector": {
            "tolerations": [node_scheduling.CONTROL_PLANE_TOLERATION],
            "resources": {
                "limits": {
                    "cpu": "100m",
                    # cainjector caches every CA-injected Certificate/CRD/webhook/APIService
                    # cluster-wide; 128Mi OOMKilled it in a crash-loop on this cluster.
                    "memory": "512Mi",
                },
                "requests": {"cpu": "10m", "memory": "128Mi"},
            },
        },
    }


def _service_monitor(chart: Chart, name: str, *, component: str, port: str) -> None:
    ServiceMonitor(
        chart,
        name,
        metadata=ApiObjectMetadata(name=name, namespace=NAMESPACE),
        selector=ServiceMonitorSpecSelector(
            match_labels={"app.kubernetes.io/instance": NAME, "app.kubernetes.io/component": component}
        ),
        # ServiceMonitor.port matches the Service port name, not the targetPort.
        endpoints=[Endpoint.plain(port=port)],
    )


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    k8s.KubeNamespace(
        chart,
        "namespace",
        metadata=k8s.ObjectMeta(name=NAMESPACE, labels={"rbac.ducktape.io/agent-readable-logs": "true"}),
    )
    repository = HelmRepository(
        chart,
        "repository",
        metadata=ApiObjectMetadata(name="jetstack", namespace="flux-system"),
        spec=HelmRepositorySpec(interval="24h", url="https://charts.jetstack.io"),
    )
    helm_release(
        chart,
        NAME,
        NAMESPACE,
        repository=repository,
        chart="cert-manager",
        version="v1.21.2",
        interval="30m",
        chart_interval="12h",
        install=RETRY_FAILED_INSTALL,
        values=_values(),
    )
    _service_monitor(chart, "cert-manager", component="controller", port="tcp-prometheus-servicemonitor")
    _service_monitor(chart, "cert-manager-webhook", component="webhook", port="metrics")
    return chart


def cert_manager(chart: Chart, directory: RenderedDirectory, monitoring_crds: Kustomization) -> Kustomization:
    return flux_kustomization(
        chart,
        NAME,
        directory,
        timeout="5m",
        depends_on=flux_kustomization_depends_on_many(
            # the ServiceMonitor/PodMonitor CRD
            monitoring_crds
        ),
    )
