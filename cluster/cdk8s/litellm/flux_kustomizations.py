"""Flux Kustomizations for the cluster/k8s/litellm slice."""

from __future__ import annotations

from cdk8s import Chart
from flux_kustomize.io.fluxcd.toolkit.kustomize import (
    KustomizationSpec,
    KustomizationSpecDecryption,
    KustomizationSpecDecryptionProvider,
    KustomizationSpecDecryptionSecretRef,
    KustomizationSpecDeletionPolicy,
    KustomizationSpecHealthChecks,
    KustomizationSpecSourceRef,
    KustomizationSpecSourceRefKind,
)

from cluster.cdk8s.flux import Kustomization, flux_kustomization, flux_kustomization_depends_on_many


def litellm_db(
    chart: Chart, litellm_namespace: Kustomization, cnpg: Kustomization, local_path_provisioner: Kustomization
) -> Kustomization:
    name = "litellm-db"
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
            path="./cluster/k8s/litellm/db",
            prune=False,
            deletion_policy=KustomizationSpecDeletionPolicy.ORPHAN,
            wait=True,
            depends_on=flux_kustomization_depends_on_many(litellm_namespace, cnpg, local_path_provisioner),
        ),
    )


# The litellm-keys Terraform CR lives DOWNSTREAM of the litellm app, not in
# litellm-secrets: minting virtual keys needs a serving LiteLLM with its
# virtual-key DB. Coupling the TF's health into litellm-secrets (the app's
# dependency) deadlocked the 2026-07-02 rollout — the app never applied the
# DATABASE_URL deployment because its secrets layer waited on a TF apply that
# needed the app. Dependency direction here is the fix.
def litellm_keys_tf(
    chart: Chart, litellm: Kustomization, tofu_controller: Kustomization, tofu_state_db: Kustomization
) -> Kustomization:
    name = "litellm-keys-tf"
    return flux_kustomization(
        chart,
        name,
        spec=KustomizationSpec(
            retry_interval="1m",
            interval="10m",
            timeout="10m",
            path="./cluster/k8s/litellm/keys-tf",
            prune=True,
            # Decrypt litellm-clients-sops-age-key.sops.yaml (the narrow SOPS_AGE_KEY for
            # the tf-runner) so sops_file in tf/gitops/litellm-keys can read the virtual-key
            # SSOT. Added when that SOPS file arrived — previously this dir held only plain YAML.
            decryption=KustomizationSpecDecryption(
                provider=KustomizationSpecDecryptionProvider.SOPS,
                secret_ref=KustomizationSpecDecryptionSecretRef(name="sops-age-cluster-secrets"),
            ),
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            wait=True,
            health_checks=[
                KustomizationSpecHealthChecks(
                    api_version="infra.contrib.fluxcd.io/v1alpha2",
                    kind="Terraform",
                    name="litellm-keys",
                    namespace="flux-system",
                )
            ],
            depends_on=flux_kustomization_depends_on_many(
                # The app must serve (with its DB) before keys can mint.
                litellm,
                tofu_controller,
                tofu_state_db,
            ),
        ),
    )


def litellm_namespace(chart: Chart) -> Kustomization:
    name = "litellm-namespace"
    return flux_kustomization(
        chart,
        name,
        spec=KustomizationSpec(
            interval="10m",
            path="./cluster/k8s/litellm/namespace",
            prune=False,
            deletion_policy=KustomizationSpecDeletionPolicy.ORPHAN,
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            timeout="2m",
        ),
    )


def litellm_secrets(
    chart: Chart,
    external_creds: Kustomization,
    litellm_namespace: Kustomization,
    external_secrets_config: Kustomization,
) -> Kustomization:
    name = "litellm-secrets"
    return flux_kustomization(
        chart,
        name,
        spec=KustomizationSpec(
            retry_interval="1m",
            interval="10m",
            path="./cluster/k8s/litellm/secrets",
            prune=False,
            deletion_policy=KustomizationSpecDeletionPolicy.ORPHAN,
            decryption=KustomizationSpecDecryption(
                provider=KustomizationSpecDecryptionProvider.SOPS,
                secret_ref=KustomizationSpecDecryptionSecretRef(name="sops-age-cluster-secrets"),
            ),
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            timeout="5m",
            depends_on=flux_kustomization_depends_on_many(external_creds, litellm_namespace, external_secrets_config),
        ),
    )
