"""Flux Kustomizations for the cluster/k8s/cpap-sync slice."""

from __future__ import annotations

from cdk8s import Chart
from flux_kustomize.io.fluxcd.toolkit.kustomize import (
    KustomizationSpec,
    KustomizationSpecDecryption,
    KustomizationSpecDecryptionProvider,
    KustomizationSpecDecryptionSecretRef,
    KustomizationSpecHealthChecks,
    KustomizationSpecSourceRef,
    KustomizationSpecSourceRefKind,
)

from cluster.cdk8s.flux import Kustomization, flux_kustomization, flux_kustomization_depends_on_many


def cpap_sync(
    chart: Chart, external_secrets_config: Kustomization, kubevirt: Kustomization, forgejo_images: Kustomization
) -> Kustomization:
    name = "cpap-sync"
    return flux_kustomization(
        chart,
        name,
        spec=KustomizationSpec(
            interval="10m",
            retry_interval="1m",
            path="./cluster/k8s/cpap-sync",
            prune=True,
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            decryption=KustomizationSpecDecryption(
                provider=KustomizationSpecDecryptionProvider.SOPS,
                secret_ref=KustomizationSpecDecryptionSecretRef(name="sops-age-cluster-secrets"),
            ),
            timeout="30m",
            depends_on=flux_kustomization_depends_on_many(external_secrets_config, kubevirt, forgejo_images),
            wait=True,
            health_checks=[
                KustomizationSpecHealthChecks(
                    api_version="kubevirt.io/v1", kind="VirtualMachine", name="cpap-gateway", namespace="cpap-sync"
                )
            ],
        ),
    )
