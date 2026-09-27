"""Flux Kustomizations for the cluster/k8s/snapshot-controller slice."""

from __future__ import annotations

from cdk8s import Chart
from flux_kustomize.io.fluxcd.toolkit.kustomize import (
    KustomizationSpecImages,
    KustomizationSpecPatches,
    KustomizationSpecPatchesTarget,
    KustomizationSpecSourceRef,
    KustomizationSpecSourceRefKind,
)

from cluster.cdk8s.flux import Kustomization, flux_kustomization


def snapshot_controller(chart: Chart) -> Kustomization:
    name = "snapshot-controller"
    return flux_kustomization(
        chart,
        name,
        KustomizationSpecSourceRef(
            kind=KustomizationSpecSourceRefKind.GIT_REPOSITORY,
            name="external-snapshotter-source",
            namespace="ducktape-flux",
        ),
        path="./deploy/kubernetes/snapshot-controller",
        timeout="5m",
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
    )


def snapshot_controller_crds(chart: Chart) -> Kustomization:
    name = "snapshot-controller-crds"
    return flux_kustomization(
        chart,
        name,
        KustomizationSpecSourceRef(
            kind=KustomizationSpecSourceRefKind.GIT_REPOSITORY,
            name="external-snapshotter-source",
            namespace="ducktape-flux",
        ),
        interval="1h",
        path="./client/config/crd",
        prune=False,
        timeout="2m",
    )
