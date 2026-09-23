"""The flux-system Secrets the github-secrets-sync Terraform reads, copied by ESO from
their canonical external-creds sources.

Hand-written beside the generated output: `ci-age-key.sops.yaml`.
"""

from __future__ import annotations

from pathlib import Path

from cdk8s import App, Chart
from cdk8s_plus_34 import k8s
from external_secrets_crds.io.external_secrets import (
    ExternalSecret,
    ExternalSecretSpec,
    ExternalSecretSpecData,
    ExternalSecretSpecDataRemoteRef,
    ExternalSecretSpecSecretStoreRef,
    ExternalSecretSpecSecretStoreRefKind,
    ExternalSecretSpecTarget,
    ExternalSecretSpecTargetCreationPolicy,
    ExternalSecretSpecTargetDeletionPolicy,
    ExternalSecretSpecTargetTemplate,
    ExternalSecretSpecTargetTemplateMetadata,
)
from flux_kustomize.io.fluxcd.toolkit.kustomize import KustomizationSpec, KustomizationSpecHealthChecks
from source_watcher_crds.io.fluxcd.extensions.source import ArtifactGeneratorSpecArtifacts

from cluster.cdk8s.artifact_generators import artifact_path, artifact_source_ref
from cluster.cdk8s.flux import (
    SOPS_DECRYPTION,
    Kustomization,
    flux_kustomization,
    flux_kustomization_depends_on_many,
    kustomize_kustomization,
)
from cluster.cdk8s.generation import write_charts, write_yaml
from cluster.cdk8s.metadata import metadata

NAME = "github-secrets-sync-secrets"
OUTPUT_DIR = "cluster/k8s/github-secrets-sync/secrets"
_NAMESPACE = "flux-system"
_CI_AGE_KEY_FILE = "ci-age-key.sops.yaml"


def _external_secret(
    chart: Chart,
    id: str,
    *,
    name: str,
    secret_key: str,
    source: str,
    template: ExternalSecretSpecTargetTemplate | None = None,
) -> ExternalSecret:
    return ExternalSecret(
        chart,
        id,
        metadata=metadata(name, _NAMESPACE),
        spec=ExternalSecretSpec(
            refresh_interval="1h",
            secret_store_ref=ExternalSecretSpecSecretStoreRef(
                kind=ExternalSecretSpecSecretStoreRefKind.CLUSTER_SECRET_STORE,
                name="kubernetes-external-creds-secret-store",
            ),
            target=ExternalSecretSpecTarget(
                name=name,
                creation_policy=ExternalSecretSpecTargetCreationPolicy.OWNER,
                deletion_policy=ExternalSecretSpecTargetDeletionPolicy.RETAIN,
                template=template,
            ),
            data=[
                ExternalSecretSpecData(
                    secret_key=secret_key, remote_ref=ExternalSecretSpecDataRemoteRef(key=source, property=secret_key)
                )
            ],
        ),
    )


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    # Preserve the established flux-system Secret contract for GitOps controllers
    # and Reflector while the canonical encrypted PAT lives in external-creds.
    k8s.KubeServiceAccount(
        chart, "external-creds-reader", metadata=k8s.ObjectMeta(name="external-creds-reader", namespace=_NAMESPACE)
    )
    _external_secret(
        chart,
        "pat",
        name="github-secrets-sync-pat",
        secret_key="token",
        source="github-agentydragon-2",
        template=ExternalSecretSpecTargetTemplate(
            type="Opaque",
            metadata=ExternalSecretSpecTargetTemplateMetadata(
                annotations={
                    "description": (
                        "Fine-grained GitHub PAT for GitOps-managed GitHub resources and token-rotation commits."
                        " Required permissions and rationale are documented in"
                        " cluster/k8s/github-secrets-sync/README.md."
                    )
                }
            ),
        ),
    )
    # Terraform reads this flux-system copy when publishing the GitHub Actions secret.
    _external_secret(
        chart, "buildbuddy-api-key", name="buildbuddy-api-key", secret_key="api-key", source="buildbuddy-api-key"
    )
    return chart


def write_manifests(root: Path) -> None:
    write_charts(root, OUTPUT_DIR, chart)
    write_yaml(
        root / OUTPUT_DIR / "kustomization.yaml",
        kustomize_kustomization(resources=[_CI_AGE_KEY_FILE, f"{NAME}.k8s.yaml"]),
    )


def github_secrets_sync_secrets(
    chart: Chart,
    artifact: ArtifactGeneratorSpecArtifacts,
    external_creds: Kustomization,
    external_secrets_config: Kustomization,
) -> Kustomization:
    return flux_kustomization(
        chart,
        NAME,
        spec=KustomizationSpec(
            interval="10m",
            retry_interval="1m",
            path=artifact_path(artifact),
            # CLEANUP: restore pruning after ESO owns flux-system/github-secrets-sync-pat
            # and the old SOPS inventory entry has been retired safely.
            prune=False,
            source_ref=artifact_source_ref(artifact),
            timeout="2m",
            depends_on=flux_kustomization_depends_on_many(external_creds, external_secrets_config),
            health_checks=[
                KustomizationSpecHealthChecks(
                    api_version="external-secrets.io/v1",
                    kind="ExternalSecret",
                    name="github-secrets-sync-pat",
                    namespace=_NAMESPACE,
                ),
                KustomizationSpecHealthChecks(
                    api_version="external-secrets.io/v1",
                    kind="ExternalSecret",
                    name="buildbuddy-api-key",
                    namespace=_NAMESPACE,
                ),
            ],
            decryption=SOPS_DECRYPTION,
        ),
    )
