"""Install the BuildBuddy Crossplane provider for the LiteLLM input Secret."""

from cdk8s import ApiObjectMetadata, App, Chart
from crossplane_deploymentruntimeconfig_crds.io.crossplane.pkg import (
    DeploymentRuntimeConfig,
    DeploymentRuntimeConfigSpec,
    DeploymentRuntimeConfigSpecDeploymentTemplate,
    DeploymentRuntimeConfigSpecDeploymentTemplateSpec,
    DeploymentRuntimeConfigSpecDeploymentTemplateSpecSelector,
    DeploymentRuntimeConfigSpecDeploymentTemplateSpecTemplate,
    DeploymentRuntimeConfigSpecDeploymentTemplateSpecTemplateSpec,
    DeploymentRuntimeConfigSpecDeploymentTemplateSpecTemplateSpecContainers,
    DeploymentRuntimeConfigSpecDeploymentTemplateSpecTemplateSpecContainersEnv,
    DeploymentRuntimeConfigSpecDeploymentTemplateSpecTemplateSpecContainersVolumeMounts,
    DeploymentRuntimeConfigSpecDeploymentTemplateSpecTemplateSpecVolumes,
    DeploymentRuntimeConfigSpecDeploymentTemplateSpecTemplateSpecVolumesConfigMap,
    DeploymentRuntimeConfigSpecServiceAccountTemplate,
    DeploymentRuntimeConfigSpecServiceAccountTemplateMetadata,
)
from crossplane_pkg_provider_crds.io.crossplane.pkg import (
    Provider,
    ProviderSpec,
    ProviderSpecPackagePullSecrets,
    ProviderSpecRuntimeConfigRef,
)

from cluster.cdk8s import crossplane, public_web_pki
from cluster.cdk8s.flux import Kustomization, RenderedDirectory, flux_kustomization, flux_kustomization_depends_on_many
from cluster.cdk8s.forgejo_registry.pull_credentials import SECRET_NAME as FORGEJO_PULL_SECRET
from cluster.cdk8s.manifest_roots import GENERATED_ROOT, HAND_WRITTEN_ROOT

NAME = "crossplane-provider-buildbuddy"
OUTPUT_DIR = f"{GENERATED_ROOT}/{NAME}"
PINS_DIR = f"{HAND_WRITTEN_ROOT}/{NAME}-image-pins"
NAMESPACE = "litellm"
SOURCE_SECRET = "litellm-key-buildbuddy-live-tests"
CREDENTIAL_SECRET = "buildbuddy-crossplane-provider-credentials"
_SERVICE_ACCOUNT = "provider-buildbuddy"


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    runtime = DeploymentRuntimeConfig(
        chart,
        "runtime",
        metadata=ApiObjectMetadata(name="buildbuddy"),
        spec=DeploymentRuntimeConfigSpec(
            service_account_template=DeploymentRuntimeConfigSpecServiceAccountTemplate(
                metadata=DeploymentRuntimeConfigSpecServiceAccountTemplateMetadata(name=_SERVICE_ACCOUNT)
            ),
            deployment_template=DeploymentRuntimeConfigSpecDeploymentTemplate(
                spec=DeploymentRuntimeConfigSpecDeploymentTemplateSpec(
                    selector=DeploymentRuntimeConfigSpecDeploymentTemplateSpecSelector(),
                    template=DeploymentRuntimeConfigSpecDeploymentTemplateSpecTemplate(
                        spec=DeploymentRuntimeConfigSpecDeploymentTemplateSpecTemplateSpec(
                            containers=[
                                DeploymentRuntimeConfigSpecDeploymentTemplateSpecTemplateSpecContainers(
                                    name="package-runtime",
                                    volume_mounts=[
                                        DeploymentRuntimeConfigSpecDeploymentTemplateSpecTemplateSpecContainersVolumeMounts(
                                            name=public_web_pki.NAME,
                                            mount_path=public_web_pki.MOUNT_PATH,
                                            read_only=True,
                                        )
                                    ],
                                    env=[
                                        DeploymentRuntimeConfigSpecDeploymentTemplateSpecTemplateSpecContainersEnv(
                                            name="SSL_CERT_FILE", value=public_web_pki.CA_FILE
                                        ),
                                        DeploymentRuntimeConfigSpecDeploymentTemplateSpecTemplateSpecContainersEnv(
                                            name="WATCH_NAMESPACE", value=NAMESPACE
                                        ),
                                        DeploymentRuntimeConfigSpecDeploymentTemplateSpecTemplateSpecContainersEnv(
                                            name="WATCH_SECRET", value=SOURCE_SECRET
                                        ),
                                        DeploymentRuntimeConfigSpecDeploymentTemplateSpecTemplateSpecContainersEnv(
                                            name="CREDENTIAL_SECRET", value=CREDENTIAL_SECRET
                                        ),
                                    ],
                                )
                            ],
                            volumes=[
                                DeploymentRuntimeConfigSpecDeploymentTemplateSpecTemplateSpecVolumes(
                                    name=public_web_pki.NAME,
                                    config_map=DeploymentRuntimeConfigSpecDeploymentTemplateSpecTemplateSpecVolumesConfigMap(
                                        name=public_web_pki.NAME
                                    ),
                                )
                            ],
                        )
                    ),
                )
            ),
        ),
    )
    Provider(
        chart,
        "provider",
        metadata=ApiObjectMetadata(name="provider-buildbuddy"),
        spec=ProviderSpec(
            package="git.allegedly.works/ducktape-ci/crossplane-provider-buildbuddy:latest",
            package_pull_secrets=[ProviderSpecPackagePullSecrets(name=FORGEJO_PULL_SECRET)],
            revision_activation_policy="Automatic",
            runtime_config_ref=ProviderSpecRuntimeConfigRef(name=runtime.name),
        ),
    )
    return chart


def resources(
    chart: Chart,
    directory: RenderedDirectory,
    litellm_provider: Kustomization,
    litellm: Kustomization,
    cert_manager_environment: Kustomization,
) -> Kustomization:
    return flux_kustomization(
        chart,
        NAME,
        directory,
        timeout="15m",
        prune=False,
        wait=True,
        health_check_exprs=[crossplane.PROVIDER_HEALTH],
        depends_on=flux_kustomization_depends_on_many(litellm_provider, litellm, cert_manager_environment),
        description="Install the BuildBuddy provider after Crossplane and its registry credentials are ready.",
    )
