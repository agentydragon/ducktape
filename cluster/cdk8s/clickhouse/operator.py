"""The Altinity ClickHouse operator: the clickhouse Namespace, the chart's HelmRepository
and the HelmRelease that installs it, scoped to that one namespace.

`operator-values.sops.yaml`, hand-written beside the generated output, carries the
chart values that must stay encrypted; the HelmRelease reads it through `valuesFrom`.
"""

from __future__ import annotations

from pathlib import Path

from cdk8s import App, Chart
from flux_helm.io.fluxcd.toolkit.helm import (
    HelmReleaseSpecDriftDetection,
    HelmReleaseSpecDriftDetectionMode,
    HelmReleaseSpecValuesFrom,
    HelmReleaseSpecValuesFromKind,
)
from flux_kustomize.io.fluxcd.toolkit.kustomize import KustomizationSpecDeletionPolicy
from flux_source.io.fluxcd.toolkit.source import HelmRepository, HelmRepositorySpec, HelmRepositorySpecType
from source_watcher_crds.io.fluxcd.extensions.source import ArtifactGeneratorSpecArtifacts

from cluster.cdk8s.flux import (
    SOPS_DECRYPTION,
    Kustomization,
    flux_kustomization,
    flux_kustomization_depends_on,
    kustomize_kustomization,
)
from cluster.cdk8s.generation import write_charts, write_namespace, write_yaml
from cluster.cdk8s.helm import helm_release
from cluster.cdk8s.manifest_roots import HAND_WRITTEN_ROOT
from cluster.cdk8s.metadata import metadata

NAME = "clickhouse-operator"
NAMESPACE = "clickhouse"
OUTPUT_DIR = f"{HAND_WRITTEN_ROOT}/clickhouse/operator"
_RESTRICTED_CONTAINER = {
    "allowPrivilegeEscalation": False,
    "capabilities": {"drop": ["ALL"]},
    "runAsNonRoot": True,
    "seccompProfile": {"type": "RuntimeDefault"},
}
_KUBECTL_UID = 65532


def _values() -> dict[str, object]:
    return {
        "watchNamespaces": [NAMESPACE],
        "rbac": {"namespaceScoped": True},
        "crdHook": {
            # The chart invokes /bin/sh in this image; the official kubectl image is distroless.
            "image": {
                "repository": "bitnamilegacy/kubectl",
                "tag": "1.33.4@sha256:ed0b31a0508da84ee655c5c6e01bd3897fc56ad6cf69debb27fa1893a06d2246",
            },
            "resources": {"requests": {"cpu": "25m", "memory": "32Mi"}, "limits": {"cpu": "200m", "memory": "128Mi"}},
            "podSecurityContext": {
                "runAsNonRoot": True,
                "runAsUser": _KUBECTL_UID,
                "seccompProfile": {"type": "RuntimeDefault"},
            },
            "containerSecurityContext": {**_RESTRICTED_CONTAINER, "runAsUser": _KUBECTL_UID},
        },
        "operator": {
            "resources": {"requests": {"cpu": "50m", "memory": "128Mi"}, "limits": {"cpu": "500m", "memory": "512Mi"}},
            "containerSecurityContext": _RESTRICTED_CONTAINER,
        },
        "metrics": {
            "enabled": True,
            "resources": {"requests": {"cpu": "25m", "memory": "64Mi"}, "limits": {"cpu": "200m", "memory": "256Mi"}},
            "containerSecurityContext": _RESTRICTED_CONTAINER,
        },
        # Altinity images declare USER nobody; use its numeric UID for runAsNonRoot validation.
        "podSecurityContext": {"runAsNonRoot": True, "runAsUser": 65534, "seccompProfile": {"type": "RuntimeDefault"}},
        "serviceMonitor": {"enabled": True},
    }


def helmrelease_chart(app: App) -> Chart:
    chart = Chart(app, "helmrelease", disable_resource_name_hashes=True)
    repository = HelmRepository(
        chart,
        "repository",
        metadata=metadata("altinity-clickhouse-operator", "flux-system"),
        spec=HelmRepositorySpec(
            type=HelmRepositorySpecType.OCI, url="oci://ghcr.io/altinity/clickhouse-operator-helm-chart", interval="12h"
        ),
    )
    helm_release(
        chart,
        NAME,
        NAMESPACE,
        repository=repository,
        chart="altinity-clickhouse-operator",
        version="0.27.3",
        interval="30m",
        chart_interval="12h",
        timeout="10m",
        drift_detection=HelmReleaseSpecDriftDetection(mode=HelmReleaseSpecDriftDetectionMode.ENABLED),
        values_from=[
            HelmReleaseSpecValuesFrom(
                # operator-values.sops.yaml
                kind=HelmReleaseSpecValuesFromKind.SECRET,
                name="clickhouse-operator-values",
                values_key="values.yaml",
            )
        ],
        values=_values(),
    )
    return chart


def write_manifests(root: Path) -> None:
    write_namespace(
        root,
        OUTPUT_DIR,
        name=NAMESPACE,
        labels={
            "goldilocks.fairwinds.com/enabled": "true",
            # ClickHouse and Keeper have topology-aware, capacity-planned requests.
            # VPA admission raised ClickHouse from 500m to 1554m, which made its
            # local-PV-pinned replica unschedulable. Keep recommendations visible in
            # Goldilocks without mutating operator-managed Pods.
            "goldilocks.fairwinds.com/vpa-update-mode": "off",
            "rbac.ducktape.io/agent-readable-logs": "true",
            "pod-security.kubernetes.io/enforce": "baseline",
            "pod-security.kubernetes.io/audit": "restricted",
            "pod-security.kubernetes.io/warn": "restricted",
        },
    )
    write_charts(root, OUTPUT_DIR, helmrelease_chart)
    write_yaml(
        root / OUTPUT_DIR / "kustomization.yaml",
        kustomize_kustomization(resources=["namespace.k8s.yaml", "operator-values.sops.yaml", "helmrelease.k8s.yaml"]),
    )


def clickhouse_operator(
    chart: Chart, artifact: ArtifactGeneratorSpecArtifacts, monitoring_crds: Kustomization
) -> Kustomization:
    return flux_kustomization(
        chart,
        NAME,
        artifact,
        deletion_policy=KustomizationSpecDeletionPolicy.ORPHAN,
        decryption=SOPS_DECRYPTION,
        timeout="10m",
        # The chart enables ServiceMonitor resources.
        depends_on=[flux_kustomization_depends_on(monitoring_crds)],
    )
