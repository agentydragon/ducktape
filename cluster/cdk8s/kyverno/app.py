"""Kyverno itself: its Namespace and the HelmRepository/HelmRelease installing the chart."""

from __future__ import annotations

from cdk8s import App, Chart

from cluster.cdk8s import namespaces, node_scheduling
from cluster.cdk8s.flux import Kustomization, RenderedDirectory, flux_kustomization
from cluster.cdk8s.helm import RETRY_FAILED_INSTALL, helm_release, https_helm_repository
from cluster.cdk8s.manifest_roots import GENERATED_ROOT
from cluster.cdk8s.namespaces import VPA_UPDATE_MODE_LABEL, Vpa
from cluster.cdk8s.vpa_policy import (
    VPA_RESOURCE_POLICY_ANNOTATION,
    ContainerPolicy,
    ControlledValues,
    resource_policy_annotation,
)

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


def _initial_memory_annotations(
    container_name: str, *, cpu_request: str, memory_floor: str, memory_limit: str
) -> dict[str, str]:
    return {
        VPA_UPDATE_MODE_LABEL: Vpa.INITIAL,
        VPA_RESOURCE_POLICY_ANNOTATION: resource_policy_annotation(
            ContainerPolicy(
                container_name=container_name,
                controlled_values=ControlledValues.REQUESTS_ONLY,
                controlled_resources=("memory",),
                # VPA 1.7.1 admission can still use a stale CPU target from Off mode.
                min_allowed={"cpu": cpu_request, "memory": memory_floor},
                max_allowed={"cpu": cpu_request, "memory": memory_limit},
            ),
            ContainerPolicy(controlled_values=ControlledValues.REQUESTS_ONLY, mode="Off"),
        ),
    }


def _values() -> dict[str, object]:
    return {
        # Use ghcr.io instead of reg.kyverno.io (scarf.sh proxy) — more reliable.
        "global": {"image": {"registry": "ghcr.io"}},
        # HA: one replica per control-plane node (3 nodes = 3 replicas).
        # Ensures the webhook is reachable from every API server without
        # cross-node hops that can fail during early bootstrap.
        "admissionController": {
            "replicas": 3,
            # Keep two ready admission endpoints during voluntary evictions.
            "podDisruptionBudget": {"minAvailable": 2},
            # Deployment metadata keeps the Pod template unchanged. Peak 30-day
            # working set was 206Mi, including 158Mi in the first 10 minutes.
            "annotations": _initial_memory_annotations(
                "kyverno",
                cpu_request=_ADMISSION_CPU_REQUEST,
                memory_floor="224Mi",
                memory_limit=_ADMISSION_MEMORY_LIMIT,
            ),
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
            # Soft anti-affinity: prefer spreading replicas across control-plane nodes.
            # Replaces topologySpreadConstraints whose labelSelector didn't match
            # (pods have instance=kyverno-kyverno, not instance=kyverno), causing
            # all 3 replicas to stack on a single control-plane node.
            # The chart default uses preferredDuringScheduling with the correct labels.
            "antiAffinity": {"enabled": True},
            # Use kyverno's INTERNAL cert management, not external cert-manager.
            #
            # WHY: Kyverno's startup sequence (cmd/kyverno/main.go:922-934) is:
            #   1. server.Run()      — HTTPS server starts on :9443
            #   2. le.Run()          — leader election
            #   3. certmanager ctrl  — creates CA + TLS secrets (leader controller, line 304)
            #   4. webhook ctrl      — creates VWC with caBundle (leader controller, line 305)
            #
            # With internal certs (enabled=false):
            #   The webhook-controller (pkg/controllers/webhook/controller.go:621-634)
            #   creates the VWC dynamically by reading the CA secret and calling
            #   vwcClient.Create(). This only happens AFTER the certmanager controller
            #   creates certs in step 3. So VWC existence = webhook ready to serve.
            #   The readiness probe (pkg/utils/runtime/utils.go:52) validates TLS
            #   certs against CA — returns 200 only when certs are valid.
            #   The TLS server uses GetCertificate callback (pkg/webhooks/server.go:277)
            #   reading from informer cache — auto-refreshes, no static files.
            #
            # With cert-manager (enabled=true):
            #   The Helm chart creates cert-manager Certificate resources as static
            #   templates (templates/admission-controller/cert-manager-certificates.yaml).
            #   cert-manager must issue certs before kyverno can serve TLS, adding a
            #   dependency on cert-manager. But the VWC is still created dynamically
            #   by the webhook-controller — the problem is that the Deployment readiness
            #   probe passes as soon as cert-manager provisions the TLS secret, BEFORE
            #   the webhook-controller has created the VWC. This causes a window where
            #   Flux marks kyverno as Ready (Deployment available) but the VWC doesn't
            #   exist yet — then the VWC appears moments later and the API server
            #   starts routing admission requests while downstream HelmReleases are
            #   already being installed, causing webhook timeouts.
            #
            # Trust model: Only the API server calls webhooks. The VWC contains the
            # caBundle field — the API server uses it to verify kyverno's self-signed
            # TLS cert. No cluster-wide trust or cert-manager needed.
            #
            # Refs:
            #   Source: github.com/kyverno/kyverno (cloned at /code/github.com/kyverno/kyverno)
            #   Flux kstatus treats VWC as "unknown"=pass: https://github.com/fluxcd/flux2/issues/4346
            #   Kyverno webhook timeouts blocking workloads: https://github.com/kyverno/kyverno/issues/1950
            "certManager": {"enabled": False},
        },
        # Background controller for policy reports
        "backgroundController": {
            "enabled": True,
            "replicas": 1,
            "priorityClassName": "system-cluster-critical",
            # Peak 30-day working set was 191Mi during startup.
            "annotations": _initial_memory_annotations(
                "controller",
                cpu_request=_BACKGROUND_CPU_REQUEST,
                memory_floor="208Mi",
                memory_limit=_BACKGROUND_MEMORY_LIMIT,
            ),
            "resources": {
                "limits": {"cpu": "200m", "memory": _BACKGROUND_MEMORY_LIMIT},
                "requests": {"cpu": _BACKGROUND_CPU_REQUEST, "memory": "64Mi"},
            },
        },
        "cleanupController": {
            "enabled": True,
            "replicas": 1,
            "priorityClassName": "system-cluster-critical",
            # Peak 30-day working set was 123Mi, close to the existing 128Mi limit.
            "annotations": _initial_memory_annotations(
                "controller", cpu_request=_CLEANUP_CPU_REQUEST, memory_floor="124Mi", memory_limit=_CLEANUP_MEMORY_LIMIT
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
            "annotations": _initial_memory_annotations(
                "controller",
                cpu_request=_REPORTS_CPU_REQUEST,
                memory_floor=_REPORTS_MEMORY_REQUEST,
                memory_limit=_REPORTS_MEMORY_LIMIT,
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
