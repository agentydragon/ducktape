"""Flux Kustomizations for the cluster/k8s/external-secrets slice."""

from __future__ import annotations

from cdk8s import Chart
from flux_kustomize.io.fluxcd.toolkit.kustomize import (
    KustomizationSpec,
    KustomizationSpecHealthChecks,
    KustomizationSpecSourceRef,
    KustomizationSpecSourceRefKind,
)

from cluster.cdk8s.flux import (
    Kustomization,
    flux_kustomization,
    flux_kustomization_depends_on,
    flux_kustomization_depends_on_many,
)


def external_secrets_config(chart: Chart, external_secrets_operator: Kustomization) -> Kustomization:
    name = "external-secrets-config"
    return flux_kustomization(
        chart,
        name,
        spec=KustomizationSpec(
            interval="10m0s",
            retry_interval="30s",
            path="./cluster/k8s/external-secrets/config",
            prune=True,
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            timeout="5m0s",
            wait=True,
            # Health-check a representative shared ClusterSecretStore before dependents run.
            # Application-scoped stores are owned and checked by their app Kustomizations.
            health_checks=[
                KustomizationSpecHealthChecks(
                    api_version="external-secrets.io/v1",
                    kind="ClusterSecretStore",
                    name="kubernetes-flux-system-secret-store",
                )
            ],
            depends_on=[flux_kustomization_depends_on(external_secrets_operator)],
        ),
    )


def external_secrets_crds(chart: Chart) -> Kustomization:
    name = "external-secrets-crds"
    return flux_kustomization(
        chart,
        name,
        spec=KustomizationSpec(
            retry_interval="1m",
            interval="1h",
            # CRDs from the external-secrets repository
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.GIT_REPOSITORY,
                name="external-secrets-source",
                namespace="ducktape-flux",
            ),
            path="./deploy/crds",
            prune=False,  # Don't delete CRDs on uninstall (safety)
            wait=True,
            timeout="2m",
        ),
    )


def external_secrets_operator(
    chart: Chart, external_secrets_crds: Kustomization, cert_manager: Kustomization
) -> Kustomization:
    name = "external-secrets-operator"
    return flux_kustomization(
        chart,
        name,
        spec=KustomizationSpec(
            retry_interval="1m",
            interval="10m0s",
            path="./cluster/k8s/external-secrets/operator",
            prune=True,
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            timeout="5m0s",
            wait=True,
            depends_on=flux_kustomization_depends_on_many(
                # CRDs must be in kustomize-controller cache first
                external_secrets_crds,
                # ESO uses Issuer resources
                cert_manager,
            ),
            health_checks=[
                KustomizationSpecHealthChecks(
                    api_version="helm.toolkit.fluxcd.io/v2",
                    kind="HelmRelease",
                    name="external-secrets",
                    namespace="external-secrets-system",
                ),
                KustomizationSpecHealthChecks(
                    api_version="apps/v1",
                    kind="Deployment",
                    name="external-secrets",
                    namespace="external-secrets-system",
                ),
                KustomizationSpecHealthChecks(
                    api_version="apps/v1",
                    kind="Deployment",
                    name="external-secrets-webhook",
                    namespace="external-secrets-system",
                ),
                # Ensure both webhook configurations are registered before dependents create resources
                KustomizationSpecHealthChecks(
                    api_version="admissionregistration.k8s.io/v1",
                    kind="ValidatingWebhookConfiguration",
                    name="externalsecret-validate",
                ),
                KustomizationSpecHealthChecks(
                    api_version="admissionregistration.k8s.io/v1",
                    kind="ValidatingWebhookConfiguration",
                    name="secretstore-validate",
                ),
            ],
        ),
    )
