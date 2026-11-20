"""cert-manager: its Namespace, the jetstack HelmRepository, the HelmRelease, and the
ServiceMonitors for the controller and webhook metrics Services the chart creates.

`values` is an untyped dict: Helm values carry no schema for `cdk8s_import` to ingest.
"""

from __future__ import annotations

from cdk8s import ApiObjectMetadata, App, Chart, Yaml
from flux_helm.io.fluxcd.toolkit.helm import (
    HelmReleaseSpecPostRenderers,
    HelmReleaseSpecPostRenderersKustomize,
    HelmReleaseSpecPostRenderersKustomizePatches,
    HelmReleaseSpecPostRenderersKustomizePatchesTarget,
)
from prometheus_operator_crds.com.coreos.monitoring import ServiceMonitor

from cluster.cdk8s import namespaces, node_scheduling
from cluster.cdk8s.cert_manager.config import LETSENCRYPT_ISSUER
from cluster.cdk8s.cert_manager.environment import NAMESPACE
from cluster.cdk8s.helm import RETRY_FAILED_INSTALL, helm_release, helm_repository_source_ref, https_helm_repository
from cluster.cdk8s.manifest_roots import GENERATED_ROOT
from cluster.cdk8s.namespaces import Vpa, VpaProfile
from cluster.cdk8s.service_ref import Pods, Port, ServiceRef
from cluster.cdk8s.vpa_policy import ContainerPolicy, ControlledValues

NAME = "cert-manager"
OUTPUT_DIR = f"{GENERATED_ROOT}/cert-manager/app"
_REPOSITORY_NAME = "jetstack"
_REPOSITORY_NAMESPACE = "flux-system"
_CPU_REQUEST = "10m"
_CONTROLLER_MEMORY_LIMIT = "128Mi"
# cainjector caches every CA-injected Certificate/CRD/webhook/APIService cluster-wide.
# A 128Mi limit previously left it in an OOMKilled crash loop.
_CAINJECTOR_MEMORY_LIMIT = "512Mi"
_WEBHOOK_MEMORY_LIMIT = "128Mi"
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


def _availability(component: str, *, replicas: int, min_available: int) -> dict[str, object]:
    return {
        "replicaCount": replicas,
        "podDisruptionBudget": {"enabled": True, "minAvailable": min_available},
        "affinity": {
            "podAntiAffinity": {
                "requiredDuringSchedulingIgnoredDuringExecution": [
                    {
                        "labelSelector": {"matchLabels": _component(component).selector},
                        "topologyKey": "kubernetes.io/hostname",
                    }
                ]
            }
        },
    }


def _readiness_patch(
    deployment: str, container: str, probe: dict[str, object]
) -> HelmReleaseSpecPostRenderersKustomizePatches:
    # The pinned chart configures readiness only for the webhook.
    return HelmReleaseSpecPostRenderersKustomizePatches(
        target=HelmReleaseSpecPostRenderersKustomizePatchesTarget(
            kind="Deployment", name=deployment, namespace=NAMESPACE
        ),
        patch=Yaml.stringify(
            {
                "apiVersion": "apps/v1",
                "kind": "Deployment",
                "metadata": {"name": deployment, "namespace": NAMESPACE},
                "spec": {"template": {"spec": {"containers": [{"name": container, "readinessProbe": probe}]}}},
            }
        ),
    )


def _values() -> dict[str, object]:
    return {
        **_availability("controller", replicas=2, min_available=1),
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
        "resources": {
            "limits": {"cpu": "100m", "memory": _CONTROLLER_MEMORY_LIMIT},
            "requests": {"cpu": _CPU_REQUEST, "memory": "32Mi"},
        },
        "webhook": {
            **_availability("webhook", replicas=3, min_available=2),
            "tolerations": [node_scheduling.CONTROL_PLANE_TOLERATION],
            # Additional DNS names for the webhook certificate.
            "extraArgs": [
                "--dynamic-serving-dns-names=cert-manager-webhook,cert-manager-webhook.cert-manager,"
                "cert-manager-webhook.cert-manager.svc,cert-manager-webhook.cert-manager.svc.cluster.local"
            ],
            "resources": {
                "limits": {"cpu": "100m", "memory": _WEBHOOK_MEMORY_LIMIT},
                "requests": {"cpu": _CPU_REQUEST, "memory": "32Mi"},
            },
        },
        "cainjector": {
            **_availability("cainjector", replicas=2, min_available=1),
            "tolerations": [node_scheduling.CONTROL_PLANE_TOLERATION],
            "resources": {
                "limits": {"cpu": "100m", "memory": _CAINJECTOR_MEMORY_LIMIT},
                "requests": {"cpu": _CPU_REQUEST, "memory": "128Mi"},
            },
        },
    }


def _service_monitor(chart: Chart, name: str, service: ServiceRef) -> None:
    ServiceMonitor(
        chart, name, metadata=ApiObjectMetadata(name=name, namespace=NAMESPACE), spec=service.service_monitor_spec()
    )


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    namespaces.namespace(
        chart,
        "namespace",
        name=NAMESPACE,
        vpa=VpaProfile(
            mode=Vpa.INITIAL,
            container_policies=(
                # Floors round up the 30-day working-set peaks (104, 200, and 53 MiB, including startup).
                ContainerPolicy.memory_requests_only(
                    "cert-manager-controller",
                    cpu_request=_CPU_REQUEST,
                    memory_floor="112Mi",
                    memory_limit=_CONTROLLER_MEMORY_LIMIT,
                ),
                ContainerPolicy.memory_requests_only(
                    "cert-manager-cainjector",
                    cpu_request=_CPU_REQUEST,
                    memory_floor="224Mi",
                    memory_limit=_CAINJECTOR_MEMORY_LIMIT,
                ),
                ContainerPolicy.memory_requests_only(
                    "cert-manager-webhook",
                    cpu_request=_CPU_REQUEST,
                    memory_floor="64Mi",
                    memory_limit=_WEBHOOK_MEMORY_LIMIT,
                ),
                # The startupapicheck Job and any new containers stay outside the pilot.
                ContainerPolicy(controlled_values=ControlledValues.REQUESTS_ONLY, mode="Off"),
            ),
        ),
    )
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
        post_renderers=[
            HelmReleaseSpecPostRenderers(
                kustomize=HelmReleaseSpecPostRenderersKustomize(
                    patches=[
                        _readiness_patch(
                            NAME, f"{NAME}-controller", {"httpGet": {"path": "/livez", "port": "http-healthz"}}
                        ),
                        # The manager starts metrics after CRD discovery and injector registration.
                        _readiness_patch(
                            f"{NAME}-cainjector", f"{NAME}-cainjector", {"tcpSocket": {"port": "http-metrics"}}
                        ),
                        # Keep the namespace's existing request policy; allow PDB-limited updates.
                        HelmReleaseSpecPostRenderersKustomizePatches(
                            target=HelmReleaseSpecPostRenderersKustomizePatchesTarget(
                                kind="Deployment", name=f"{NAME}-cainjector", namespace=NAMESPACE
                            ),
                            patch=Yaml.stringify(
                                {
                                    "apiVersion": "apps/v1",
                                    "kind": "Deployment",
                                    "metadata": {
                                        "name": f"{NAME}-cainjector",
                                        "namespace": NAMESPACE,
                                        "annotations": {namespaces.VPA_UPDATE_MODE_LABEL: Vpa.AUTO},
                                    },
                                }
                            ),
                        ),
                    ]
                )
            )
        ],
        values=_values(),
    )
    _service_monitor(chart, NAME, _CONTROLLER_METRICS)
    _service_monitor(chart, "webhook", _WEBHOOK_METRICS)
    return chart
