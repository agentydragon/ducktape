"""The ducktape-ci Forgejo registry tenant: the `Terraform` CR provisioning it
(tf/gitops/forgejo-images) and the pull-credentials `ExternalSecret`, shared by every
generated directory that pulls a `git.allegedly.works`-hosted image.

Hand-written beside the generated output: `registry-creds.sops.yaml`, the tenant's
canonical registry credential.
"""

from pathlib import Path

from cdk8s import ApiObjectMetadata, App, Chart
from cdk8s_plus_34 import ISecret, Secret, k8s
from constructs import Construct
from external_secrets_crds.io.external_secrets import (
    ExternalSecret,
    ExternalSecretSpec,
    ExternalSecretSpecDataFrom,
    ExternalSecretSpecDataFromExtract,
    ExternalSecretSpecSecretStoreRef,
    ExternalSecretSpecSecretStoreRefKind,
    ExternalSecretSpecTarget,
    ExternalSecretSpecTargetTemplate,
    ExternalSecretSpecTargetTemplateMergePolicy,
)
from flux_kustomize.io.fluxcd.toolkit.kustomize import KustomizationSpecHealthChecks
from source_watcher_crds.io.fluxcd.extensions.source import ArtifactGeneratorSpecArtifacts

from cluster.cdk8s import terraform
from cluster.cdk8s.flux import (
    SOPS_DECRYPTION,
    Kustomization,
    flux_kustomization,
    flux_kustomization_depends_on_many,
    kustomize_kustomization,
)
from cluster.cdk8s.generation import write_charts, write_yaml

NAME = "forgejo-images"
OUTPUT_DIR = "cluster/k8s/forgejo-images"
SECRET_NAME = "forgejo-images-creds"
_REGISTRY_CREDS_FILE = "registry-creds.sops.yaml"


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    k8s.KubeNamespace(
        chart,
        "namespace",
        metadata=k8s.ObjectMeta(
            name=NAME,
            annotations={
                "description": (
                    "Holds the shared credential for the ducktape-ci Forgejo registry tenant"
                    " (CI-pushed in-cluster images) and its provisioning Terraform."
                )
            },
        ),
    )
    terraform.gitops_terraform(chart, "terraform", name=NAME, variables={})
    # Flux's own copy, for the image-automation ImageRepositories that scan the registry.
    forgejo_images_creds_external_secret(chart, "flux-system-creds", namespace="flux-system")
    return chart


def write_manifests(root: Path) -> None:
    write_charts(root, OUTPUT_DIR, chart)
    write_yaml(
        root / OUTPUT_DIR / "kustomization.yaml",
        kustomize_kustomization(resources=[f"{NAME}.k8s.yaml", _REGISTRY_CREDS_FILE]),
    )


def forgejo_images(
    chart: Chart,
    artifact: ArtifactGeneratorSpecArtifacts,
    external_secrets_config: Kustomization,
    forgejo: Kustomization,
    tofu_controller: Kustomization,
    tofu_state_db: Kustomization,
) -> Kustomization:
    return flux_kustomization(
        chart,
        NAME,
        artifact,
        timeout="10m",
        decryption=SOPS_DECRYPTION,
        health_checks=[
            KustomizationSpecHealthChecks(
                api_version="infra.contrib.fluxcd.io/v1alpha2", kind="Terraform", name=NAME, namespace="flux-system"
            )
        ],
        depends_on=flux_kustomization_depends_on_many(
            external_secrets_config,
            # Forgejo API must be up (provider target)
            forgejo,
            tofu_controller,
            tofu_state_db,
        ),
        description=(
            "ducktape-ci Forgejo registry tenant — shared credential (read by "
            "consumers, including flux-system, via per-namespace ExternalSecrets "
            "against kubernetes-forgejo-images-secret-store) + Terraform that "
            "provisions the Forgejo user."
        ),
    )


def forgejo_images_creds_external_secret(scope: Construct, id: str, *, namespace: str) -> ExternalSecret:
    return ExternalSecret(
        scope,
        id,
        metadata=ApiObjectMetadata(name=SECRET_NAME, namespace=namespace),
        spec=ExternalSecretSpec(
            refresh_interval="1h",
            secret_store_ref=ExternalSecretSpecSecretStoreRef(
                name="kubernetes-forgejo-images-secret-store",
                kind=ExternalSecretSpecSecretStoreRefKind.CLUSTER_SECRET_STORE,
            ),
            target=ExternalSecretSpecTarget(
                name=SECRET_NAME,
                template=ExternalSecretSpecTargetTemplate(
                    type="kubernetes.io/dockerconfigjson",
                    merge_policy=ExternalSecretSpecTargetTemplateMergePolicy.MERGE,
                ),
            ),
            data_from=[ExternalSecretSpecDataFrom(extract=ExternalSecretSpecDataFromExtract(key=SECRET_NAME))],
        ),
    )


def forgejo_images_creds_secret_ref(scope: Construct, id: str) -> ISecret:
    """Reference the `ExternalSecret` a sibling `forgejo_images_creds_external_secret` call created."""
    return Secret.from_secret_name(scope, id, SECRET_NAME)
