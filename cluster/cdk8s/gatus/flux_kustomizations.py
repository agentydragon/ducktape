"""Flux Kustomizations for the cluster/k8s/gatus slice."""

from __future__ import annotations

from cdk8s import Chart
from flux_kustomize.io.fluxcd.toolkit.kustomize import (
    KustomizationSpec,
    KustomizationSpecHealthChecks,
    KustomizationSpecSourceRef,
    KustomizationSpecSourceRefKind,
)

from cluster.cdk8s.flux import Kustomization, flux_kustomization, flux_kustomization_depends_on


def gatus(
    chart: Chart,
    gatus_namespace: Kustomization,
    gatus_db: Kustomization,
    gatus_sso_tf: Kustomization,
    litellm_secrets: Kustomization,
    gateway: Kustomization,
    monitoring_crds: Kustomization,
) -> Kustomization:
    name = "gatus"
    return flux_kustomization(
        chart,
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
                flux_kustomization_depends_on(gatus_namespace),
                flux_kustomization_depends_on(gatus_db),
                flux_kustomization_depends_on(gatus_sso_tf),
                flux_kustomization_depends_on(litellm_secrets),
                flux_kustomization_depends_on(gateway),
                # the ServiceMonitor/PodMonitor CRD
                flux_kustomization_depends_on(monitoring_crds),
            ],
        ),
    )


def gatus_db(chart: Chart, gatus_namespace: Kustomization, cnpg: Kustomization) -> Kustomization:
    name = "gatus-db"
    return flux_kustomization(
        chart,
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
            depends_on=[flux_kustomization_depends_on(gatus_namespace), flux_kustomization_depends_on(cnpg)],
        ),
    )


def gatus_namespace(chart: Chart) -> Kustomization:
    name = "gatus-namespace"
    return flux_kustomization(
        chart,
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


def gatus_sso_tf(
    chart: Chart, tofu_controller: Kustomization, tofu_state_db: Kustomization, authentik: Kustomization
) -> Kustomization:
    name = "gatus-sso-tf"
    return flux_kustomization(
        chart,
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
                flux_kustomization_depends_on(tofu_controller),
                flux_kustomization_depends_on(tofu_state_db),
                flux_kustomization_depends_on(authentik),
            ],
        ),
    )
