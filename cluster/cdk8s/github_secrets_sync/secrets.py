"""The flux-system Secrets the github-secrets-sync Terraform reads, copied by ESO from
their canonical external-creds sources.

Hand-written beside the generated output: `ci-age-key.sops.yaml`.
"""

from __future__ import annotations

from pathlib import Path

from cdk8s import App, Chart
from cdk8s_plus_34 import k8s
from external_secrets_crds.io.external_secrets import (
    ExternalSecretSpecTargetCreationPolicy,
    ExternalSecretSpecTargetDeletionPolicy,
    ExternalSecretSpecTargetTemplate,
    ExternalSecretSpecTargetTemplateMetadata,
)
from flux_kustomize.io.fluxcd.toolkit.kustomize import KustomizationSpecHealthChecks
from source_watcher_crds.io.fluxcd.extensions.source import ArtifactGeneratorSpecArtifacts

from cluster.cdk8s import external_creds
from cluster.cdk8s.flux import (
    SOPS_DECRYPTION,
    Kustomization,
    flux_kustomization,
    flux_kustomization_depends_on_many,
    kustomize_kustomization,
)
from cluster.cdk8s.generation import write_charts, write_yaml
from cluster.cdk8s.manifest_roots import HAND_WRITTEN_ROOT
from cluster.cdk8s.providers.external_secrets.external_secret import ExternalSecret, remote_data

NAME = "github-secrets-sync-secrets"
OUTPUT_DIR = f"{HAND_WRITTEN_ROOT}/{NAME}"
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
        name=name,
        namespace=_NAMESPACE,
        refresh="1h",
        store=external_creds.STORE,
        data=[remote_data(source, secret_key)],
        creation_policy=ExternalSecretSpecTargetCreationPolicy.OWNER,
        deletion_policy=ExternalSecretSpecTargetDeletionPolicy.RETAIN,
        template=template,
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
        artifact,
        wait=None,
        # CLEANUP: restore pruning after ESO owns flux-system/github-secrets-sync-pat
        # and the old SOPS inventory entry has been retired safely.
        prune=False,
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
    )
