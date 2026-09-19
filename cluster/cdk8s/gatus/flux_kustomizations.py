"""Flux Kustomizations for the cluster/k8s/gatus slice."""

from __future__ import annotations

from pathlib import Path

from flux_kustomize.io.fluxcd.toolkit.kustomize import (
    KustomizationSpec,
    KustomizationSpecDependsOn,
    KustomizationSpecHealthChecks,
    KustomizationSpecSourceRef,
    KustomizationSpecSourceRefKind,
)

from cluster.cdk8s.flux import flux_kustomization
from cluster.cdk8s.generation import write_yaml


def gatus() -> dict[str, object]:
    name = "gatus"
    return flux_kustomization(
        name,
        spec=KustomizationSpec(
            retry_interval="1m",
            interval="10m",
            timeout="10m",
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            path="./cluster/k8s/gatus/app",
            prune=True,
            wait=True,
            health_checks=[
                KustomizationSpecHealthChecks(
                    api_version="helm.toolkit.fluxcd.io/v2", kind="HelmRelease", name="gatus", namespace="gatus"
                )
            ],
            depends_on=[
                KustomizationSpecDependsOn(name="gatus-namespace", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="gatus-db", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="gatus-sso-tf", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="litellm-secrets", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="gateway", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(
                    name="monitoring-crds",  # the ServiceMonitor/PodMonitor CRD
                    namespace="ducktape-flux",
                ),
            ],
        ),
    )


def gatus_db() -> dict[str, object]:
    name = "gatus-db"
    return flux_kustomization(
        name,
        spec=KustomizationSpec(
            retry_interval="1m",
            interval="10m",
            timeout="5m",
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            path="./cluster/k8s/gatus/db",
            prune=True,
            wait=True,
            health_checks=[
                KustomizationSpecHealthChecks(
                    api_version="postgresql.cnpg.io/v1", kind="Cluster", name="gatus-db", namespace="gatus"
                )
            ],
            depends_on=[
                KustomizationSpecDependsOn(name="gatus-namespace", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="cnpg", namespace="ducktape-flux"),
            ],
        ),
    )


def gatus_namespace() -> dict[str, object]:
    name = "gatus-namespace"
    return flux_kustomization(
        name,
        spec=KustomizationSpec(
            retry_interval="1m",
            interval="10m",
            timeout="5m",
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            path="./cluster/k8s/gatus/namespace",
            prune=True,
            wait=True,
            health_checks=[KustomizationSpecHealthChecks(api_version="v1", kind="Namespace", name="gatus")],
        ),
    )


def gatus_sso_tf() -> dict[str, object]:
    name = "gatus-sso-tf"
    return flux_kustomization(
        name,
        spec=KustomizationSpec(
            retry_interval="1m",
            interval="10m",
            path="./cluster/k8s/gatus/sso-tf",
            prune=True,
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            health_checks=[
                KustomizationSpecHealthChecks(
                    api_version="infra.contrib.fluxcd.io/v1alpha2",
                    kind="Terraform",
                    name="gatus-sso",
                    namespace="flux-system",
                )
            ],
            timeout="10m",
            depends_on=[
                KustomizationSpecDependsOn(name="tofu-controller", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="tofu-state-db", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="authentik", namespace="ducktape-flux"),
            ],
        ),
    )


def write_manifests(root: Path) -> None:
    path = root / "cluster/k8s/gatus/app/flux-kustomization.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(path, gatus())
    path = root / "cluster/k8s/gatus/db/flux-kustomization.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(path, gatus_db())
    path = root / "cluster/k8s/gatus/namespace/flux-kustomization.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(path, gatus_namespace())
    path = root / "cluster/k8s/gatus/sso-tf/flux-kustomization.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(path, gatus_sso_tf())
