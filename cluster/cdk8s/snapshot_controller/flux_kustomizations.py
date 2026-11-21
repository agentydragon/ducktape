"""Flux Kustomizations for the snapshot-controller component."""

from __future__ import annotations

from textwrap import dedent

from cdk8s import Chart
from flux_kustomize.io.fluxcd.toolkit.kustomize import (
    KustomizationSpecImages,
    KustomizationSpecPatches,
    KustomizationSpecPatchesTarget,
    KustomizationSpecSourceRef,
    KustomizationSpecSourceRefKind,
)

from cluster.cdk8s import node_scheduling
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
                patch=dedent(f"""\
                    apiVersion: apps/v1
                    kind: Deployment
                    metadata:
                      name: {name}
                      namespace: kube-system
                    spec:
                      strategy:
                        rollingUpdate:
                          maxSurge: 1
                          maxUnavailable: 0
                      template:
                        spec:
                          nodeSelector:
                            {node_scheduling.ZONE_LABEL}: {node_scheduling.HIL_OVH_ZONE}
                          affinity:
                            podAntiAffinity:
                              requiredDuringSchedulingIgnoredDuringExecution:
                                - topologyKey: kubernetes.io/hostname
                                  labelSelector:
                                    matchLabels:
                                      app.kubernetes.io/name: {name}
                    """),
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
