"""Manage the BuildBuddy live-test secret with Crossplane."""

from buildbuddy_providerconfig_crds.works.allegedly.m.buildbuddy import (
    ProviderConfig,
    ProviderConfigSpec,
    ProviderConfigSpecCredentials,
    ProviderConfigSpecCredentialsSecretRef,
)
from buildbuddy_secret_crds.works.allegedly.m.buildbuddy import (
    Secret,
    SecretSpec,
    SecretSpecForProvider,
    SecretSpecForProviderValueSecretRef,
    SecretSpecProviderConfigRef,
)
from cdk8s import ApiObjectMetadata, App, Chart
from cdk8s_plus_34 import ServiceAccount
from external_secrets_crds.io.external_secrets import ExternalSecretSpecTargetCreationPolicy

from cluster.cdk8s import buildbuddy_provider
from cluster.cdk8s.external_secrets.single_secret_store import single_secret_store
from cluster.cdk8s.flux import Kustomization, RenderedDirectory, flux_kustomization, flux_kustomization_depends_on_many
from cluster.cdk8s.manifest_roots import GENERATED_ROOT
from cluster.cdk8s.providers.external_secrets.external_secret import ExternalSecret, SecretStoreRef, remote_data

NAME = "litellm-buildbuddy-crossplane"
OUTPUT_DIR = f"{GENERATED_ROOT}/{NAME}"


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    reader = ServiceAccount(
        chart,
        "credentials-reader",
        metadata=ApiObjectMetadata(
            name="buildbuddy-provider-credentials-reader", namespace=buildbuddy_provider.NAMESPACE
        ),
        automount_token=False,
    )
    store = single_secret_store(
        chart,
        "buildbuddy-provider-credentials",
        reader=reader,
        source_namespace="flux-system",
        source_secret="buildbuddy-org-admin-api-key",
        consumer_namespace=buildbuddy_provider.NAMESPACE,
    )
    ExternalSecret(
        chart,
        "provider-credentials",
        metadata=ApiObjectMetadata(name=buildbuddy_provider.CREDENTIAL_SECRET, namespace=buildbuddy_provider.NAMESPACE),
        refresh_interval="1h",
        secret_store_ref=SecretStoreRef.cluster(store),
        data=[remote_data("buildbuddy-org-admin-api-key", "api-key", secret_key="api-key")],
        creation_policy=ExternalSecretSpecTargetCreationPolicy.OWNER,
    )
    ProviderConfig(
        chart,
        "provider-config",
        metadata=ApiObjectMetadata(name="default", namespace=buildbuddy_provider.NAMESPACE),
        spec=ProviderConfigSpec(
            credentials=ProviderConfigSpecCredentials(
                source="Secret",
                secret_ref=ProviderConfigSpecCredentialsSecretRef(
                    name=buildbuddy_provider.CREDENTIAL_SECRET, namespace=buildbuddy_provider.NAMESPACE, key="api-key"
                ),
            )
        ),
    )
    Secret(
        chart,
        "live-tests",
        metadata=ApiObjectMetadata(
            name="live-tests",
            namespace=buildbuddy_provider.NAMESPACE,
            annotations={"crossplane.io/external-name": "OPENAI_API_KEY"},
        ),
        spec=SecretSpec(
            for_provider=SecretSpecForProvider(
                value_secret_ref=SecretSpecForProviderValueSecretRef(
                    name=buildbuddy_provider.SOURCE_SECRET, key="api-key"
                )
            ),
            provider_config_ref=SecretSpecProviderConfigRef(kind="ProviderConfig", name="default"),
            management_policies=["Observe", "Create", "Update"],
        ),
    )
    return chart


def resources(
    chart: Chart,
    directory: RenderedDirectory,
    provider: Kustomization,
    crossplane_keys: Kustomization,
    keys_tf: Kustomization,
) -> Kustomization:
    return flux_kustomization(
        chart,
        NAME,
        directory,
        wait=False,
        depends_on=flux_kustomization_depends_on_many(provider, crossplane_keys, keys_tf),
        description="Manage the BuildBuddy live-test secret with Crossplane.",
    )
