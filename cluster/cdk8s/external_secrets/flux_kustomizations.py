"""Flux Kustomizations for the cluster/k8s/external-secrets slice."""

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


def external_secrets_config() -> dict[str, object]:
    name = "external-secrets-config"
    return flux_kustomization(
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
            depends_on=[KustomizationSpecDependsOn(name="external-secrets-operator", namespace="ducktape-flux")],
        ),
    )


def external_secrets_crds() -> dict[str, object]:
    name = "external-secrets-crds"
    return flux_kustomization(
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


def external_secrets_operator() -> dict[str, object]:
    name = "external-secrets-operator"
    return flux_kustomization(
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
            depends_on=[
                KustomizationSpecDependsOn(
                    name="external-secrets-crds",  # CRDs must be in kustomize-controller cache first
                    namespace="ducktape-flux",
                ),
                KustomizationSpecDependsOn(
                    name="cert-manager",  # ESO uses Issuer resources
                    namespace="ducktape-flux",
                ),
            ],
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


def write_manifests(root: Path) -> None:
    path = root / "cluster/k8s/external-secrets/config/flux-kustomization.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(path, external_secrets_config())
    path = root / "cluster/k8s/external-secrets/crds/flux-kustomization.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(path, external_secrets_crds())
    path = root / "cluster/k8s/external-secrets/operator/flux-kustomization.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(path, external_secrets_operator())
