"""Kyverno itself: its Namespace and the HelmRepository/HelmRelease installing the chart."""

from __future__ import annotations

from cdk8s import App, Chart

from cluster.cdk8s import namespaces, node_scheduling
from cluster.cdk8s.flux import Kustomization, RenderedDirectory, flux_kustomization
from cluster.cdk8s.helm import RETRY_FAILED_INSTALL, helm_release, https_helm_repository
from cluster.cdk8s.manifest_roots import GENERATED_ROOT
from cluster.cdk8s.namespaces import VPA_UPDATE_MODE_LABEL, Vpa, initial_request_annotations
from cluster.cdk8s.vpa_policy import ContainerPolicy

NAME = "kyverno"
OUTPUT_DIR = f"{GENERATED_ROOT}/kyverno/app"
_FLUX_NAMESPACE = "flux-system"
_ADMISSION_CPU_REQUEST = "100m"
_ADMISSION_MEMORY_LIMIT = "384Mi"
_BACKGROUND_CPU_REQUEST = "50m"
_BACKGROUND_MEMORY_LIMIT = "256Mi"
_CLEANUP_CPU_REQUEST = "10m"
_CLEANUP_MEMORY_LIMIT = "128Mi"
_REPORTS_CPU_REQUEST = "100m"
_REPORTS_MEMORY_REQUEST = "256Mi"
_REPORTS_MEMORY_LIMIT = "512Mi"


def _values() -> dict[str, object]:
    return {
        # Use ghcr.io instead of reg.kyverno.io (scarf.sh proxy) — more reliable.
        "global": {"image": {"registry": "ghcr.io"}},
        # Three webhook replicas on the control plane.
        "admissionController": {
            "replicas": 3,
            # Keep two ready admission endpoints during voluntary evictions.
            "podDisruptionBudget": {"minAvailable": 2},
            "updateStrategy": {"rollingUpdate": {"maxUnavailable": 0}},
            # Deployment metadata keeps the Pod template unchanged. Peak 30-day
            # working set was 206Mi, including 158Mi in the first 10 minutes.
            "annotations": {
                **initial_request_annotations(
                    ContainerPolicy.memory_requests_only(
                        "kyverno",
                        cpu_request=_ADMISSION_CPU_REQUEST,
                        memory_floor="224Mi",
                        memory_limit=_ADMISSION_MEMORY_LIMIT,
                    )
                ),
                VPA_UPDATE_MODE_LABEL: Vpa.AUTO,
            },
            "container": {
                "resources": {
                    "limits": {"memory": _ADMISSION_MEMORY_LIMIT},
                    "requests": {"cpu": _ADMISSION_CPU_REQUEST, "memory": "128Mi"},
                }
            },
            # All four controllers carry this. Without a priority class they sort
            # with ordinary workloads under kubelet eviction, and a node whose disk was
            # filled by something else entirely evicts them for a few KiB apiece — which
            # is how the background and cleanup controllers went down on 2026-09-07. The
            # admission controller is the acute one: its webhook gates API writes, so
            # losing it is a cluster-wide outage rather than degraded reporting.
            "priorityClassName": "system-cluster-critical",
            "nodeSelector": {node_scheduling.CONTROL_PLANE_TAINT_KEY: ""},
            "tolerations": [node_scheduling.CONTROL_PLANE_TOLERATION],
            # Prefer separate hosts from old Pods while rolling. Require each new
            # ReplicaSet to spread across the control-plane nodes it can use.
            "antiAffinity": {"enabled": True},
            "topologySpreadConstraints": [
                {
                    "maxSkew": 1,
                    "topologyKey": "kubernetes.io/hostname",
                    "whenUnsatisfiable": "DoNotSchedule",
                    "nodeTaintsPolicy": "Honor",
                    "labelSelector": {
                        "matchLabels": {
                            "app.kubernetes.io/component": "admission-controller",
                            "app.kubernetes.io/instance": "{{ .Release.Name }}",
                        }
                    },
                    "matchLabelKeys": ["pod-template-hash"],
                }
            ],
            # Kyverno's internal certificates, not cert-manager. The webhook controller creates the
            # ValidatingWebhookConfiguration only after the certificate controller has made the CA
            # and TLS Secrets, so that configuration exists exactly when the webhook can serve. With
            # cert-manager the Deployment turns Ready once the TLS Secret exists, before the
            # configuration does. Flux's kstatus treats the missing configuration as passing
            # (https://github.com/fluxcd/flux2/issues/4346), so downstream HelmReleases install while
            # admission starts routing and their webhook calls time out
            # (https://github.com/kyverno/kyverno/issues/1950). Only the API server calls the
            # webhooks, verifying the self-signed certificate through the configuration's caBundle.
            "certManager": {"enabled": False},
        },
        # Background controller for policy reports
        "backgroundController": {
            "enabled": True,
            "replicas": 1,
            "priorityClassName": "system-cluster-critical",
            # Peak 30-day working set was 191Mi during startup.
            "annotations": initial_request_annotations(
                ContainerPolicy.memory_requests_only(
                    "controller",
                    cpu_request=_BACKGROUND_CPU_REQUEST,
                    memory_floor="208Mi",
                    memory_limit=_BACKGROUND_MEMORY_LIMIT,
                )
            ),
            "resources": {
                "limits": {"cpu": "200m", "memory": _BACKGROUND_MEMORY_LIMIT},
                "requests": {"cpu": _BACKGROUND_CPU_REQUEST, "memory": "64Mi"},
            },
        },
        "cleanupController": {
            "enabled": True,
            # Each replica serves policy validation and cleanup webhooks. The
            # elected leader maintains certificates and schedules cleanup jobs.
            "replicas": 3,
            "podDisruptionBudget": {"minAvailable": 2},
            "updateStrategy": {"rollingUpdate": {"maxUnavailable": 0}},
            "topologySpreadConstraints": [
                {
                    "maxSkew": 1,
                    "topologyKey": "kubernetes.io/hostname",
                    "whenUnsatisfiable": "DoNotSchedule",
                    "nodeTaintsPolicy": "Honor",
                    "labelSelector": {
                        "matchLabels": {
                            "app.kubernetes.io/component": "cleanup-controller",
                            "app.kubernetes.io/instance": "{{ .Release.Name }}",
                        }
                    },
                    "matchLabelKeys": ["pod-template-hash"],
                }
            ],
            "priorityClassName": "system-cluster-critical",
            # Peak 30-day working set was 123Mi, close to the existing 128Mi limit.
            "annotations": initial_request_annotations(
                ContainerPolicy.memory_requests_only(
                    "controller",
                    cpu_request=_CLEANUP_CPU_REQUEST,
                    memory_floor="124Mi",
                    memory_limit=_CLEANUP_MEMORY_LIMIT,
                )
            ),
            "resources": {
                "limits": {"cpu": "100m", "memory": _CLEANUP_MEMORY_LIMIT},
                "requests": {"cpu": _CLEANUP_CPU_REQUEST, "memory": "32Mi"},
            },
        },
        "reportsController": {
            "enabled": True,
            "replicas": 1,
            "priorityClassName": "system-cluster-critical",
            # Preserve the existing 256Mi reservation. Peak 30-day working set was 204Mi.
            "annotations": initial_request_annotations(
                ContainerPolicy.memory_requests_only(
                    "controller",
                    cpu_request=_REPORTS_CPU_REQUEST,
                    memory_floor=_REPORTS_MEMORY_REQUEST,
                    memory_limit=_REPORTS_MEMORY_LIMIT,
                )
            ),
            "resources": {
                # No CPU limit: this controller syncs an informer cache over ALL resources
                # (--skipResourceFilters=true) + runs background scans; the object
                # deserialization is CPU-heavy. A 100m limit throttled cache sync to ~83s,
                # blowing the leader-election lease deadline -> crashloop. Matches the
                # admission-controller (also uncapped CPU). Memory raised to hold the cache.
                "limits": {"memory": _REPORTS_MEMORY_LIMIT},
                "requests": {"cpu": _REPORTS_CPU_REQUEST, "memory": _REPORTS_MEMORY_REQUEST},
            },
        },
        "crds": {"install": True},
        "features": {
            "logging": {"format": "text"},
            "admissionReports": {"enabled": True},
            "policyReports": {"enabled": True},
        },
        # Exclude system namespaces from policy enforcement
        "config": {"excludeKyvernoNamespace": True},
    }


def chart(app: App) -> Chart:
    chart = Chart(app, "kyverno", disable_resource_name_hashes=True)
    # Keep namespace-wide VPA in recommendation mode, including the Helm migration Job.
    namespace = namespaces.namespace(chart, "namespace", name=NAME, vpa=Vpa.RECOMMEND)
    helm_release(
        chart,
        NAME,
        _FLUX_NAMESPACE,
        repository=https_helm_repository(chart, NAME, _FLUX_NAMESPACE, url="https://kyverno.github.io/kyverno/"),
        chart="kyverno",
        # MODULE.bazel pins the kyverno.io CRD bindings to this chart's appVersion.
        version="3.9.1",
        interval="15m",
        install=RETRY_FAILED_INSTALL,
        target_namespace=namespace.name,
        values=_values(),
    )
    return chart


def kyverno(chart: Chart, directory: RenderedDirectory) -> Kustomization:
    return flux_kustomization(
        chart,
        NAME,
        directory,
        interval="10m0s",
        timeout="10m0s",
        # No dependsOn: kyverno manages its own TLS via internal certmanager-controller
        # (certManager.enabled: false in the values above). No cert-manager dependency.
        #
        # Gotcha: `wait` does not cover the resource VWC. kyverno's webhook-controller
        # creates it at runtime (see the certManager note above), so it is not an
        # applied object and dependents can reach admission before it exists.
    )
