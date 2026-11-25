"""Refresh the GitHub installation token for the optional staging archive grant."""

from cdk8s import ApiObjectMetadata, App, Chart
from eso_github_access_token_crds.io.external_secrets.generators import (
    GithubAccessToken,
    GithubAccessTokenSpec,
    GithubAccessTokenSpecAuth,
    GithubAccessTokenSpecAuthPrivateKey,
    GithubAccessTokenSpecAuthPrivateKeySecretRef,
)
from external_secrets_crds.io.external_secrets import (
    ExternalSecretSpecDataFrom,
    ExternalSecretSpecDataFromSourceRef,
    ExternalSecretSpecDataFromSourceRefGeneratorRef,
    ExternalSecretSpecDataFromSourceRefGeneratorRefKind,
    ExternalSecretSpecTargetTemplate,
    ExternalSecretSpecTargetTemplateMetadata,
)
from flux_kustomize.io.fluxcd.toolkit.kustomize import (
    KustomizationSpecPostBuild,
    KustomizationSpecPostBuildSubstituteFrom,
    KustomizationSpecPostBuildSubstituteFromKind,
)

from cluster.cdk8s.agentplane.egress_credentials import STAGING_CREDENTIALS_NAMESPACE
from cluster.cdk8s.image_automation import GITHUB_APP_NAMESPACE, GITHUB_APP_SECRET
from cluster.cdk8s.manifest_roots import GENERATED_ROOT
from cluster.cdk8s.providers.external_secrets.external_secret import ExternalSecret
from cluster.cdk8s.reflector import mirror_annotations

NAME = "gaffer-private-archive"
OUTPUT_DIR = f"{GENERATED_ROOT}/agentplane-staging/{NAME}"


def github_app_ids() -> KustomizationSpecPostBuild:
    # Flux substitutes into YAML before converting to JSON. Preserve the generator's string-typed IDs.
    return KustomizationSpecPostBuild(
        substitute={"quote": '"'},
        substitute_from=[
            KustomizationSpecPostBuildSubstituteFrom(
                kind=KustomizationSpecPostBuildSubstituteFromKind.SECRET, name=GITHUB_APP_SECRET
            )
        ],
    )


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    metadata = ApiObjectMetadata(name=NAME, namespace=GITHUB_APP_NAMESPACE)
    generator = GithubAccessToken(
        chart,
        "generator",
        metadata=metadata,
        spec=GithubAccessTokenSpec(
            app_id="${quote}${githubAppID}${quote}",
            install_id="${quote}${githubAppInstallationID}${quote}",
            repositories=["gaffer-private"],
            permissions={"contents": "read"},
            auth=GithubAccessTokenSpecAuth(
                private_key=GithubAccessTokenSpecAuthPrivateKey(
                    secret_ref=GithubAccessTokenSpecAuthPrivateKeySecretRef(
                        name=GITHUB_APP_SECRET, key="githubAppPrivateKey"
                    )
                )
            ),
        ),
    )
    ExternalSecret(
        chart,
        "token",
        metadata=metadata,
        refresh_interval="30m",
        data_from=[
            ExternalSecretSpecDataFrom(
                source_ref=ExternalSecretSpecDataFromSourceRef(
                    generator_ref=ExternalSecretSpecDataFromSourceRefGeneratorRef(
                        api_version="generators.external-secrets.io/v1alpha1",
                        kind=ExternalSecretSpecDataFromSourceRefGeneratorRefKind.GITHUB_ACCESS_TOKEN,
                        name=generator.name,
                    )
                )
            )
        ],
        template=ExternalSecretSpecTargetTemplate(
            data={"token": "{{ .token }}"},
            metadata=ExternalSecretSpecTargetTemplateMetadata(
                annotations=mirror_annotations([STAGING_CREDENTIALS_NAMESPACE])
            ),
        ),
    )
    return chart
