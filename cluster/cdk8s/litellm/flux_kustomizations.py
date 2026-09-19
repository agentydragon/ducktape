"""Flux Kustomizations for the cluster/k8s/litellm slice."""

from __future__ import annotations

from pathlib import Path

from flux_kustomize.io.fluxcd.toolkit.kustomize import (
    KustomizationSpec,
    KustomizationSpecDecryption,
    KustomizationSpecDecryptionProvider,
    KustomizationSpecDecryptionSecretRef,
    KustomizationSpecDependsOn,
    KustomizationSpecHealthChecks,
    KustomizationSpecSourceRef,
    KustomizationSpecSourceRefKind,
)

from cluster.cdk8s.flux import flux_kustomization
from cluster.cdk8s.generation import write_yaml


def litellm_db() -> dict[str, object]:
    name = "litellm-db"
    return flux_kustomization(
        name,
        spec=KustomizationSpec(
            retry_interval="1m",
            interval="10m",
            timeout="5m",
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            path="./cluster/k8s/litellm/db",
            prune=True,
            wait=True,
            depends_on=[
                KustomizationSpecDependsOn(name="litellm-namespace", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="cnpg", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="local-path-provisioner", namespace="ducktape-flux"),
            ],
        ),
    )


# The litellm-keys Terraform CR lives DOWNSTREAM of the litellm app, not in
# litellm-secrets: minting virtual keys needs a serving LiteLLM with its
# virtual-key DB. Coupling the TF's health into litellm-secrets (the app's
# dependency) deadlocked the 2026-07-02 rollout — the app never applied the
# DATABASE_URL deployment because its secrets layer waited on a TF apply that
# needed the app. Dependency direction here is the fix.
def litellm_keys_tf() -> dict[str, object]:
    name = "litellm-keys-tf"
    return flux_kustomization(
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
            depends_on=[
                # The app must serve (with its DB) before keys can mint.
                KustomizationSpecDependsOn(name="litellm", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="tofu-controller", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="tofu-state-db", namespace="ducktape-flux"),
            ],
        ),
    )


def litellm_namespace() -> dict[str, object]:
    name = "litellm-namespace"
    return flux_kustomization(
        name,
        spec=KustomizationSpec(
            interval="10m",
            path="./cluster/k8s/litellm/namespace",
            prune=True,
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            timeout="2m",
        ),
    )


def litellm_secrets() -> dict[str, object]:
    name = "litellm-secrets"
    return flux_kustomization(
        name,
        spec=KustomizationSpec(
            retry_interval="1m",
            interval="10m",
            path="./cluster/k8s/litellm/secrets",
            prune=True,
            decryption=KustomizationSpecDecryption(
                provider=KustomizationSpecDecryptionProvider.SOPS,
                secret_ref=KustomizationSpecDecryptionSecretRef(name="sops-age-cluster-secrets"),
            ),
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            timeout="5m",
            depends_on=[
                KustomizationSpecDependsOn(name="external-creds", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="litellm-namespace", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="external-secrets-config", namespace="ducktape-flux"),
            ],
        ),
    )


def write_manifests(root: Path) -> None:
    path = root / "cluster/k8s/litellm/db/flux-kustomization.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(path, litellm_db())
    path = root / "cluster/k8s/litellm/keys-tf/flux-kustomization.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(path, litellm_keys_tf())
    path = root / "cluster/k8s/litellm/namespace/flux-kustomization.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(path, litellm_namespace())
    path = root / "cluster/k8s/litellm/secrets/flux-kustomization.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(path, litellm_secrets())
