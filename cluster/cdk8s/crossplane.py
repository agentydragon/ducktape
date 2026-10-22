"""Crossplane's core controller, installed before any Crossplane packages or resources."""

from __future__ import annotations

from cdk8s import ApiObject, ApiObjectMetadata, App, Chart, JsonPatch
from flux_kustomize.io.fluxcd.toolkit.kustomize import KustomizationSpecHealthCheckExprs

from cluster.cdk8s import namespaces
from cluster.cdk8s.flux import Kustomization, RenderedDirectory, flux_kustomization, flux_kustomization_depends_on
from cluster.cdk8s.helm import RETRY_FAILED_INSTALL, helm_release, https_helm_repository
from cluster.cdk8s.manifest_roots import GENERATED_ROOT
from cluster.cdk8s.namespaces import Vpa

NAME = "crossplane"
NAMESPACE = "crossplane-system"
OUTPUT_DIR = f"{GENERATED_ROOT}/{NAME}"
VERSION = "2.4.2"
LITELLM_PROVIDER_KUSTOMIZATION_NAME = "crossplane-provider-litellm"
LITELLM_PROVIDER_OUTPUT_DIR = f"{GENERATED_ROOT}/{LITELLM_PROVIDER_KUSTOMIZATION_NAME}"
LITELLM_PROVIDER_PACKAGE = "xpkg.upbound.io/thecodingsheikh/provider-litellm:v2.1.0"
_LITELLM_PROVIDER_HEALTH = KustomizationSpecHealthCheckExprs(
    api_version="pkg.crossplane.io/v1",
    kind="Provider",
    current="has(status.conditions) && status.conditions.exists(c, c.type == 'Healthy' && c.status == 'True')",
    failed="has(status.conditions) && status.conditions.exists(c, c.type == 'Healthy' && c.status == 'False')",
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


def litellm_provider_chart(app: App) -> Chart:
    """Install the Upjet-generated provider for LiteLLM's management API."""
    chart = Chart(app, LITELLM_PROVIDER_KUSTOMIZATION_NAME, disable_resource_name_hashes=True)
    ApiObject(
        chart,
        "provider",
        api_version="pkg.crossplane.io/v1",
        kind="Provider",
        metadata=ApiObjectMetadata(name="provider-litellm"),
    ).add_json_patch(
        JsonPatch.add("/spec", {"package": LITELLM_PROVIDER_PACKAGE, "revisionActivationPolicy": "Automatic"})
    )
    return chart


def litellm_provider(chart: Chart, directory: RenderedDirectory, crossplane: Kustomization) -> Kustomization:
    """Wait for the LiteLLM provider CRDs and controller before applying managed resources."""
    return flux_kustomization(
        chart,
        LITELLM_PROVIDER_KUSTOMIZATION_NAME,
        directory,
        timeout="15m",
        prune=False,
        wait=True,
        health_check_exprs=[_LITELLM_PROVIDER_HEALTH],
        depends_on=[flux_kustomization_depends_on(crossplane)],
        description="Install the LiteLLM Upjet provider after Crossplane core; LiteLLM resources reconcile separately.",
    )
