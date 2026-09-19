"""Flux Kustomizations for the cluster/k8s/snapshot-controller slice."""

from __future__ import annotations

from pathlib import Path

from flux_kustomize.io.fluxcd.toolkit.kustomize import (
    KustomizationSpec,
    KustomizationSpecDependsOn,
    KustomizationSpecHealthChecks,
    KustomizationSpecImages,
    KustomizationSpecPatches,
    KustomizationSpecPatchesTarget,
    KustomizationSpecSourceRef,
    KustomizationSpecSourceRefKind,
)

from cluster.cdk8s.flux import flux_kustomization
from cluster.cdk8s.generation import write_yaml


def snapshot_controller() -> dict[str, object]:
    name = "snapshot-controller"
    return flux_kustomization(
        name,
        spec=KustomizationSpec(
            retry_interval="1m",
            interval="10m",
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.GIT_REPOSITORY,
                name="external-snapshotter-source",
                namespace="ducktape-flux",
            ),
            path="./deploy/kubernetes/snapshot-controller",
            prune=True,
            wait=True,
            timeout="5m",
            depends_on=[KustomizationSpecDependsOn(name="snapshot-controller-crds", namespace="ducktape-flux")],
            images=[KustomizationSpecImages(name="registry.k8s.io/sig-storage/snapshot-controller", new_tag="v8.6.0")],
            patches=[
                KustomizationSpecPatches(
                    target=KustomizationSpecPatchesTarget(
                        kind="Deployment", name="snapshot-controller", namespace="kube-system"
                    ),
                    patch=(
                        "- op: add\n  path: /spec/template/spec/nodeSelector\n  value:\n    "
                        "topology.kubernetes.io/zone: hil-ovh"
                    ),
                )
            ],
            health_checks=[
                KustomizationSpecHealthChecks(
                    api_version="apps/v1", kind="Deployment", name="snapshot-controller", namespace="kube-system"
                )
            ],
        ),
    )


def snapshot_controller_crds() -> dict[str, object]:
    name = "snapshot-controller-crds"
    return flux_kustomization(
        name,
        spec=KustomizationSpec(
            retry_interval="1m",
            interval="1h",
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.GIT_REPOSITORY,
                name="external-snapshotter-source",
                namespace="ducktape-flux",
            ),
            path="./client/config/crd",
            prune=False,
            wait=True,
            timeout="2m",
        ),
    )


def write_manifests(root: Path) -> None:
    path = root / "cluster/k8s/snapshot-controller/app/flux-kustomization.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(path, snapshot_controller())
    path = root / "cluster/k8s/snapshot-controller/crds/flux-kustomization.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(path, snapshot_controller_crds())
