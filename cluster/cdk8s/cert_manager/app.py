"""cert-manager: its Namespace, the jetstack HelmRepository, the HelmRelease, and the
ServiceMonitors for the controller and webhook metrics Services the chart creates.

`values` is an untyped dict: Helm values carry no schema for `cdk8s_import` to ingest.
"""

from __future__ import annotations

from cdk8s import ApiObjectMetadata, App, Chart
from prometheus_operator_crds.com.coreos.monitoring import ServiceMonitorSpecSelector

from cluster.cdk8s import namespaces, node_scheduling
from cluster.cdk8s.cert_manager.config import LETSENCRYPT_ISSUER
from cluster.cdk8s.flux import Kustomization, RenderedDirectory, flux_kustomization, flux_kustomization_depends_on_many
from cluster.cdk8s.helm import RETRY_FAILED_INSTALL, helm_release, helm_repository_source_ref, https_helm_repository
from cluster.cdk8s.manifest_roots import GENERATED_ROOT
from cluster.cdk8s.namespaces import Vpa
from cluster.cdk8s.providers.prometheus_operator.service_monitor import Endpoint, ServiceMonitor
from cluster.cdk8s.service_ref import Pods, Port, ServiceRef

NAME = "cert-manager"
NAMESPACE = "cert-manager"
OUTPUT_DIR = f"{GENERATED_ROOT}/cert-manager/app"
_REPOSITORY_NAME = "jetstack"
_REPOSITORY_NAMESPACE = "flux-system"
# trust-manager installs from this repository too, from its own chart.
JETSTACK_SOURCE_REF = helm_repository_source_ref(_REPOSITORY_NAME, _REPOSITORY_NAMESPACE)


def _component(component: str) -> Pods:
    """One chart component's Pods; the chart labels its metrics Service the same way."""
    return Pods(
        namespace=NAMESPACE, labels=(("app.kubernetes.io/instance", NAME), ("app.kubernetes.io/component", component))
    )


# The chart's metrics Services. A ServiceMonitor endpoint names the Service port, not the targetPort.
_CONTROLLER_METRICS = ServiceRef(name=NAME, port=Port(name="http-metrics", number=9402), pods=_component("controller"))
_WEBHOOK_METRICS = ServiceRef(
    name=f"{NAME}-webhook", port=Port(name="metrics", number=9402), pods=_component("webhook")
)


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


def _service_monitor(chart: Chart, service: ServiceRef) -> None:
    ServiceMonitor(
        chart,
        service.name,
        metadata=ApiObjectMetadata(name=service.name, namespace=NAMESPACE),
        selector=ServiceMonitorSpecSelector(match_labels=service.labels),
        endpoints=[Endpoint.plain(port=service.port.name)],
    )


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    namespaces.namespace(chart, "namespace", name=NAMESPACE, vpa=Vpa.RECOMMEND)
    helm_release(
        chart,
        NAME,
        NAMESPACE,
        repository=https_helm_repository(
            chart, _REPOSITORY_NAME, _REPOSITORY_NAMESPACE, url="https://charts.jetstack.io"
        ),
        chart="cert-manager",
        version="v1.21.2",
        interval="30m",
        chart_interval="12h",
        install=RETRY_FAILED_INSTALL,
        values=_values(),
    )
    _service_monitor(chart, _CONTROLLER_METRICS)
    _service_monitor(chart, _WEBHOOK_METRICS)
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
