"""Kyverno itself: its Namespace, the HelmRepository and HelmRelease installing the chart,
and the background controller's extra RoleBinding read access."""

from __future__ import annotations

from pathlib import Path

from cdk8s import App, Chart
from cdk8s_plus_34 import k8s
from flux_source.io.fluxcd.toolkit.source import HelmRepository, HelmRepositorySpec
from source_watcher_crds.io.fluxcd.extensions.source import ArtifactGeneratorSpecArtifacts

from cluster.cdk8s.flux import Kustomization, flux_kustomization, kustomize_kustomization
from cluster.cdk8s.generation import write_charts, write_yaml
from cluster.cdk8s.helm import RETRY_FAILED_INSTALL, helm_release
from cluster.cdk8s.manifest_roots import GENERATED_ROOT
from cluster.cdk8s.metadata import metadata

NAME = "kyverno"
OUTPUT_DIR = f"{GENERATED_ROOT}/kyverno/app"
_FLUX_NAMESPACE = "flux-system"
_CONTROL_PLANE = "node-role.kubernetes.io/control-plane"


def _values() -> dict[str, object]:
    return {
        # Use ghcr.io instead of reg.kyverno.io (scarf.sh proxy) — more reliable.
        "global": {"image": {"registry": "ghcr.io"}},
        # HA: one replica per control-plane node (3 nodes = 3 replicas).
        # Ensures the webhook is reachable from every API server without
        # cross-node hops that can fail during early bootstrap.
        "admissionController": {
            "replicas": 3,
            # All four controllers carry this. Without a priority class they sort
            # with ordinary workloads under kubelet eviction, and a node whose disk was
            # filled by something else entirely evicts them for a few KiB apiece — which
            # is how the background and cleanup controllers went down on 2026-09-07. The
            # admission controller is the acute one: its webhook gates API writes, so
            # losing it is a cluster-wide outage rather than degraded reporting.
            "priorityClassName": "system-cluster-critical",
            "nodeSelector": {_CONTROL_PLANE: ""},
            "tolerations": [{"key": _CONTROL_PLANE, "operator": "Exists", "effect": "NoSchedule"}],
            # Soft anti-affinity: prefer spreading replicas across control-plane nodes.
            # Replaces topologySpreadConstraints whose labelSelector didn't match
            # (pods have instance=kyverno-kyverno, not instance=kyverno), causing
            # all 3 replicas to stack on a single control-plane node.
            # The chart default uses preferredDuringScheduling with the correct labels.
            "antiAffinity": {"enabled": True},
            "resources": {"limits": {"cpu": "500m", "memory": "512Mi"}, "requests": {"cpu": "100m", "memory": "128Mi"}},
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
            "resources": {"limits": {"cpu": "200m", "memory": "256Mi"}, "requests": {"cpu": "50m", "memory": "64Mi"}},
        },
        "cleanupController": {
            "enabled": True,
            "replicas": 1,
            "priorityClassName": "system-cluster-critical",
            "resources": {"limits": {"cpu": "100m", "memory": "128Mi"}, "requests": {"cpu": "10m", "memory": "32Mi"}},
        },
        "reportsController": {
            "enabled": True,
            "replicas": 1,
            "priorityClassName": "system-cluster-critical",
            "resources": {
                # No CPU limit: this controller syncs an informer cache over ALL resources
                # (--skipResourceFilters=true) + runs background scans; the object
                # deserialization is CPU-heavy. A 100m limit throttled cache sync to ~83s,
                # blowing the leader-election lease deadline -> crashloop. Matches the
                # admission-controller (also uncapped CPU). Memory raised to hold the cache.
                "limits": {"memory": "512Mi"},
                "requests": {"cpu": "100m", "memory": "256Mi"},
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
    namespace = k8s.KubeNamespace(chart, "namespace", metadata=k8s.ObjectMeta(name=NAME))
    repository = HelmRepository(
        chart,
        "repository",
        metadata=metadata(NAME, _FLUX_NAMESPACE),
        spec=HelmRepositorySpec(interval="24h", url="https://kyverno.github.io/kyverno/"),
    )
    helm_release(
        chart,
        NAME,
        _FLUX_NAMESPACE,
        repository=repository,
        chart="kyverno",
        # MODULE.bazel pins the kyverno.io CRD bindings to this chart's appVersion.
        version="3.9.1",
        interval="15m",
        install=RETRY_FAILED_INSTALL,
        target_namespace=namespace.name,
        values=_values(),
    )
    return chart


def background_controller_rbac_chart(app: App) -> Chart:
    chart = Chart(app, "clusterrole-background-controller-rolebindings", disable_resource_name_hashes=True)
    # Kyverno validates generate policies against the background controller's
    # permissions before admitting them. The generated diagnostics RoleBindings
    # are synchronized by that controller, so it needs to be able to read them as
    # well as the create/update/patch/delete permissions supplied by the chart.
    read = ["get", "list", "watch"]
    k8s.KubeClusterRole(
        chart,
        "rolebindings",
        metadata=k8s.ObjectMeta(
            name="kyverno-background-controller-rolebindings",
            labels={"rbac.kyverno.io/aggregate-to-background-controller": "true"},
        ),
        rules=[
            k8s.PolicyRule(api_groups=["rbac.authorization.k8s.io"], resources=["rolebindings"], verbs=read),
            # Keep the controller's permissions in step with the non-standard read-only
            # resources granted by agent-readable-namespace-metadata. Kubernetes rejects
            # a RoleBinding when the controller would grant permissions it does not hold.
            k8s.PolicyRule(api_groups=["autoscaling.k8s.io"], resources=["verticalpodautoscalers"], verbs=read),
            k8s.PolicyRule(
                api_groups=["gateway.networking.k8s.io"],
                resources=["gateways", "httproutes", "tlsroutes", "grpcroutes"],
                verbs=read,
            ),
        ],
    )
    return chart


def write_manifests(root: Path) -> None:
    write_charts(root, OUTPUT_DIR, chart, background_controller_rbac_chart)
    write_yaml(
        root / OUTPUT_DIR / "kustomization.yaml",
        kustomize_kustomization(
            resources=["kyverno.k8s.yaml", "clusterrole-background-controller-rolebindings.k8s.yaml"]
        ),
    )


def kyverno(chart: Chart, artifact: ArtifactGeneratorSpecArtifacts) -> Kustomization:
    return flux_kustomization(
        chart,
        NAME,
        artifact,
        interval="10m0s",
        timeout="10m0s",
        # No dependsOn: kyverno manages its own TLS via internal certmanager-controller
        # (certManager.enabled: false in the values above). No cert-manager dependency.
        #
        # Gotcha: `wait` does not cover the resource VWC. kyverno's webhook-controller
        # creates it at runtime (see the certManager note above), so it is not an
        # applied object and dependents can reach admission before it exists.
    )
