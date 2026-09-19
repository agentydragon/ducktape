"""Flux Kustomizations for the cluster/k8s/haku-ci slice."""

from __future__ import annotations

from pathlib import Path

from flux_kustomize.io.fluxcd.toolkit.kustomize import (
    KustomizationSpec,
    KustomizationSpecDependsOn,
    KustomizationSpecSourceRef,
    KustomizationSpecSourceRefKind,
)

from cluster.cdk8s.flux import flux_kustomization
from cluster.cdk8s.generation import write_yaml


def haku_ci() -> dict[str, object]:
    name = "haku-ci"
    return flux_kustomization(
        name,
        spec=KustomizationSpec(
            interval="10m",
            retry_interval="1m",
            timeout="5m",
            path="./cluster/k8s/haku-ci",
            prune=True,
            # The runner pod stays pending until its registration-token Secret
            # (haku-ci-runner-token) is provisioned during paving — don't block on health.
            wait=False,
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            depends_on=[
                KustomizationSpecDependsOn(
                    name="forgejo",  # Forgejo (with Actions enabled, #2556) must be up first
                    namespace="ducktape-flux",
                ),
                KustomizationSpecDependsOn(
                    name="keda",  # supplies the ScaledObject and TriggerAuthentication CRDs
                    namespace="ducktape-flux",
                ),
                # Reflector mirrors haku-forgejo-tea from the completed haku-forgejo-tea
                # Kustomization into haku-ci for the native Forgejo KEDA scaler.
                KustomizationSpecDependsOn(name="reflector", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="haku-forgejo-tea", namespace="ducktape-flux"),
            ],
        ),
    )


def write_manifests(root: Path) -> None:
    path = root / "cluster/k8s/haku-ci/flux-kustomization.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(path, haku_ci())
