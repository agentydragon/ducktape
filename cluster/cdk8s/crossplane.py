"""Crossplane's core controller, installed before any Crossplane packages or resources."""

from __future__ import annotations

from collections.abc import Sequence

from cdk8s import ApiObjectMetadata, App, Chart
from cdk8s_plus_34 import ServiceAccount
from crossplane_function_crds.io.crossplane.pkg import Function, FunctionSpec
from crossplane_pkg_provider_crds.io.crossplane.pkg import (
    Provider,
    ProviderSpec,
    ProviderSpecPackagePullSecrets,
    ProviderSpecRuntimeConfigRef,
)
from flux_kustomize.io.fluxcd.toolkit.kustomize import KustomizationSpecDependsOn, KustomizationSpecHealthCheckExprs

from cluster.cdk8s import flux, namespaces, opentofu_runtime
from cluster.cdk8s.external_secrets.single_secret_store import single_secret_store
from cluster.cdk8s.flux import Kustomization, RenderedDirectory, flux_kustomization, flux_kustomization_depends_on
from cluster.cdk8s.forgejo_registry.chart import NAME as FORGEJO_IMAGES_KUSTOMIZATION_NAME
from cluster.cdk8s.forgejo_registry.pull_credentials import (
    SECRET_NAME as FORGEJO_PULL_SECRET,
    forgejo_images_creds_external_secret,
)
from cluster.cdk8s.helm import RETRY_FAILED_INSTALL, helm_release, https_helm_repository
from cluster.cdk8s.manifest_roots import GENERATED_ROOT
from cluster.cdk8s.namespaces import Vpa

NAME = "crossplane"
NAMESPACE = "crossplane-system"
OUTPUT_DIR = f"{GENERATED_ROOT}/{NAME}"
VERSION = "2.4.2"
LITELLM_PROVIDER_KUSTOMIZATION_NAME = "crossplane-provider-litellm"
LITELLM_PROVIDER_OUTPUT_DIR = f"{GENERATED_ROOT}/{LITELLM_PROVIDER_KUSTOMIZATION_NAME}"
LITELLM_PROVIDER_PACKAGE = "git.allegedly.works/ducktape-ci/provider-litellm:v2.1.0-1.ducktape.5cc2f4308916"
OPENTOFU_PROVIDER_KUSTOMIZATION_NAME = "crossplane-provider-opentofu"
OPENTOFU_PROVIDER_OUTPUT_DIR = f"{GENERATED_ROOT}/{OPENTOFU_PROVIDER_KUSTOMIZATION_NAME}"
OPENTOFU_PROVIDER_PACKAGE = "xpkg.upbound.io/upbound/provider-opentofu:v1.1.10"
FUNCTION_GO_TEMPLATING_KUSTOMIZATION_NAME = "crossplane-function-go-templating"
FUNCTION_GO_TEMPLATING_OUTPUT_DIR = f"{GENERATED_ROOT}/{FUNCTION_GO_TEMPLATING_KUSTOMIZATION_NAME}"
FUNCTION_GO_TEMPLATING_PACKAGE = "xpkg.crossplane.io/crossplane-contrib/function-go-templating:v0.13.2"
_PROVIDER_HEALTH = KustomizationSpecHealthCheckExprs(
    api_version="pkg.crossplane.io/v1",
    kind="Provider",
    current="has(status.conditions) && status.conditions.exists(c, c.type == 'Healthy' && c.status == 'True')",
    failed="has(status.conditions) && status.conditions.exists(c, c.type == 'Healthy' && c.status == 'False')",
)
_FUNCTION_HEALTH = KustomizationSpecHealthCheckExprs(
    api_version="pkg.crossplane.io/v1",
    kind="Function",
    current="has(status.conditions) && status.conditions.exists(c, c.type == 'Healthy' && c.status == 'True')",
    failed="has(status.conditions) && status.conditions.exists(c, c.type == 'Healthy' && c.status == 'False')",
)
_FORGEJO_PULL_SECRET_HEALTH = KustomizationSpecHealthCheckExprs(
    api_version="external-secrets.io/v1",
    kind="ExternalSecret",
    current="has(status.conditions) && status.conditions.exists(c, c.type == 'Ready' && c.status == 'True')",
    failed="has(status.conditions) && status.conditions.exists(c, c.type == 'Ready' && c.status == 'False')",
)


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    namespaces.namespace(chart, "namespace", name=NAMESPACE, vpa=Vpa.RECOMMEND)
    helm_release(
        chart,
        NAME,
        NAMESPACE,
        repository=https_helm_repository(chart, NAME, NAMESPACE, url="https://charts.crossplane.io/stable"),
        chart=NAME,
        version=VERSION,
        interval="30m",
        chart_interval="12h",
        install=RETRY_FAILED_INSTALL,
        values={},
    )
    return chart


def crossplane(chart: Chart, directory: RenderedDirectory, kyverno: Kustomization) -> Kustomization:
    return flux_kustomization(
        chart,
        NAME,
        directory,
        timeout="10m",
        # Removing the owner must not uninstall the controller while Provider resources and
        # their custom-resource APIs may still be in use.
        prune=False,
        depends_on=[flux_kustomization_depends_on(kyverno)],
        description="Crossplane core controller; provider packages and managed resources reconcile separately.",
    )


def function_go_templating_chart(app: App) -> Chart:
    """Install the pinned function used by native Crossplane compositions."""
    chart = Chart(app, FUNCTION_GO_TEMPLATING_KUSTOMIZATION_NAME, disable_resource_name_hashes=True)
    Function(
        chart,
        "function-go-templating",
        metadata=ApiObjectMetadata(name="function-go-templating"),
        spec=FunctionSpec(package=FUNCTION_GO_TEMPLATING_PACKAGE),
    )
    return chart


def function_go_templating(chart: Chart, directory: RenderedDirectory, crossplane: Kustomization) -> Kustomization:
    """Install and health-check the templating Function before any Composition references it."""
    return flux_kustomization(
        chart,
        FUNCTION_GO_TEMPLATING_KUSTOMIZATION_NAME,
        directory,
        timeout="15m",
        prune=False,
        wait=True,
        health_check_exprs=[_FUNCTION_HEALTH],
        depends_on=[flux_kustomization_depends_on(crossplane)],
        description="Install the Crossplane go-templating Function before Compositions reference it.",
    )


def litellm_provider_chart(app: App) -> Chart:
    """Install the Upjet-generated provider for LiteLLM's management API."""
    chart = Chart(app, LITELLM_PROVIDER_KUSTOMIZATION_NAME, disable_resource_name_hashes=True)
    reader = ServiceAccount(
        chart,
        "forgejo-images-creds-reader",
        metadata=ApiObjectMetadata(name="crossplane-forgejo-images-creds-reader", namespace=NAMESPACE),
        automount_token=False,
    )
    store = single_secret_store(
        chart,
        "crossplane-forgejo-images-creds",
        reader=reader,
        source_namespace="forgejo-images",
        source_secret=FORGEJO_PULL_SECRET,
        consumer_namespace=NAMESPACE,
    )
    forgejo_images_creds_external_secret(chart, "forgejo-images-creds", namespace=NAMESPACE, secret_store_name=store)
    Provider(
        chart,
        "provider",
        metadata=ApiObjectMetadata(name="provider-litellm"),
        spec=ProviderSpec(
            package=LITELLM_PROVIDER_PACKAGE,
            package_pull_secrets=[ProviderSpecPackagePullSecrets(name=FORGEJO_PULL_SECRET)],
            revision_activation_policy="Automatic",
        ),
    )
    return chart


def litellm_provider(
    chart: Chart, directory: RenderedDirectory, crossplane: Kustomization, external_secrets_operator: Kustomization
) -> Kustomization:
    """Wait for the LiteLLM provider CRDs and controller before applying managed resources."""
    return flux_kustomization(
        chart,
        LITELLM_PROVIDER_KUSTOMIZATION_NAME,
        directory,
        timeout="15m",
        prune=False,
        wait=True,
        health_check_exprs=[_FORGEJO_PULL_SECRET_HEALTH, _PROVIDER_HEALTH],
        depends_on=[
            flux_kustomization_depends_on(crossplane),
            flux_kustomization_depends_on(external_secrets_operator),
            KustomizationSpecDependsOn(name=FORGEJO_IMAGES_KUSTOMIZATION_NAME, namespace=flux.NAMESPACE),
        ],
        description="Install the LiteLLM Upjet provider after Crossplane core; LiteLLM resources reconcile separately.",
    )


def opentofu_provider_chart(app: App, *, kubernetes_identities: Sequence[str]) -> Chart:
    """Install Upbound's OpenTofu provider package."""
    chart = Chart(app, OPENTOFU_PROVIDER_KUSTOMIZATION_NAME, disable_resource_name_hashes=True)
    runtime = opentofu_runtime.configure(chart, kubernetes_identities=kubernetes_identities)
    Provider(
        chart,
        "provider",
        metadata=ApiObjectMetadata(name="provider-opentofu"),
        spec=ProviderSpec(
            package=OPENTOFU_PROVIDER_PACKAGE,
            revision_activation_policy="Automatic",
            runtime_config_ref=ProviderSpecRuntimeConfigRef(name=runtime.name),
        ),
    )
    return chart


def opentofu_provider(chart: Chart, directory: RenderedDirectory, crossplane: Kustomization) -> Kustomization:
    """Wait for the OpenTofu provider CRDs and controller before applying Workspaces."""
    return flux_kustomization(
        chart,
        OPENTOFU_PROVIDER_KUSTOMIZATION_NAME,
        directory,
        timeout="15m",
        prune=False,
        wait=True,
        health_check_exprs=[_PROVIDER_HEALTH],
        depends_on=[flux_kustomization_depends_on(crossplane)],
        description="Install the OpenTofu provider after Crossplane core; its Workspace resources reconcile separately.",
    )
