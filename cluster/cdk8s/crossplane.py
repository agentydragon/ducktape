"""Crossplane's core controller, installed before any Crossplane packages or resources."""

from __future__ import annotations

from collections.abc import Sequence
from textwrap import dedent

from cdk8s import ApiObjectMetadata, App, Chart, Size
from cdk8s_plus_34 import Cpu, ServiceAccount, k8s
from crossplane_pkg_provider_crds.io.crossplane.pkg import (
    Provider,
    ProviderSpec,
    ProviderSpecPackagePullSecrets,
    ProviderSpecRuntimeConfigRef,
)
from external_secrets_crds.io.external_secrets import ExternalSecret
from flux_helm.io.fluxcd.toolkit.helm import (
    HelmReleaseSpecPostRenderers,
    HelmReleaseSpecPostRenderersKustomize,
    HelmReleaseSpecPostRenderersKustomizePatches,
    HelmReleaseSpecPostRenderersKustomizePatchesTarget,
)
from flux_kustomize.io.fluxcd.toolkit.kustomize import KustomizationSpecHealthCheckExprs

from cluster.cdk8s import namespaces, node_scheduling, opentofu_runtime
from cluster.cdk8s.external_secrets.single_secret_store import single_secret_store
from cluster.cdk8s.flux import (
    Kustomization,
    RenderedDirectory,
    flux_kustomization,
    flux_kustomization_depends_on,
    flux_kustomization_depends_on_many,
)
from cluster.cdk8s.forgejo_registry.pull_credentials import (
    SECRET_NAME as FORGEJO_PULL_SECRET,
    forgejo_images_creds_external_secret,
)
from cluster.cdk8s.helm import RETRY_FAILED_INSTALL, helm_release, https_helm_repository
from cluster.cdk8s.manifest_roots import GENERATED_ROOT
from cluster.cdk8s.namespaces import Vpa, VpaProfile
from cluster.cdk8s.vpa_policy import (
    VPA_RESOURCE_POLICY_ANNOTATION,
    ContainerPolicy,
    ControlledValues,
    resource_policy_annotation,
)

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
PROVIDER_HEALTH = KustomizationSpecHealthCheckExprs(
    api_version=Provider.GVK.api_version,
    kind=Provider.GVK.kind,
    current="has(status.conditions) && status.conditions.exists(c, c.type == 'Healthy' && c.status == 'True')",
    failed="has(status.conditions) && status.conditions.exists(c, c.type == 'Healthy' && c.status == 'False')",
)
_CORE_CPU_REQUEST = "100m"
_CORE_MEMORY_FLOOR = "256Mi"
_FORGEJO_PULL_SECRET_HEALTH = KustomizationSpecHealthCheckExprs(
    api_version=ExternalSecret.GVK.api_version,
    kind=ExternalSecret.GVK.kind,
    current="has(status.conditions) && status.conditions.exists(c, c.type == 'Ready' && c.status == 'True')",
    failed="has(status.conditions) && status.conditions.exists(c, c.type == 'Ready' && c.status == 'False')",
)


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    core_selector = {"app": NAME, "release": NAME}
    core_resource_policy = resource_policy_annotation(
        # The RBAC manager uses the same container name and a fixed 100m limit.
        ContainerPolicy(
            container_name=NAME,
            controlled_values=ControlledValues.REQUESTS_ONLY,
            controlled_resources=("cpu", "memory"),
            min_allowed={"cpu": _CORE_CPU_REQUEST, "memory": _CORE_MEMORY_FLOOR},
            max_allowed={"cpu": "500m"},
        ),
        ContainerPolicy(controlled_values=ControlledValues.REQUESTS_ONLY, mode="Off"),
        compact=True,
    )
    namespaces.namespace(
        chart,
        "namespace",
        name=NAMESPACE,
        vpa=VpaProfile(
            Vpa.INITIAL,
            (
                # Both core Deployments currently reserve 100m CPU and 256Mi memory.
                # Pin CPU while the recommender drops any stale Off-mode CPU target.
                ContainerPolicy(
                    container_name="crossplane",
                    controlled_values=ControlledValues.REQUESTS_ONLY,
                    controlled_resources=("memory",),
                    min_allowed={"cpu": _CORE_CPU_REQUEST, "memory": _CORE_MEMORY_FLOOR},
                    max_allowed={"cpu": _CORE_CPU_REQUEST},
                ),
                # Current provider runtimes have no template requests or limits; size both requests.
                ContainerPolicy(
                    container_name="package-runtime",
                    controlled_values=ControlledValues.REQUESTS_ONLY,
                    controlled_resources=("cpu", "memory"),
                    min_allowed={"cpu": Cpu.millis(15).amount, "memory": Size.mebibytes(128).as_string()},
                    max_allowed={"cpu": Cpu.millis(500).amount, "memory": Size.gibibytes(2).as_string()},
                ),
                ContainerPolicy(controlled_values=ControlledValues.REQUESTS_ONLY, mode="Off"),
            ),
        ),
    )
    k8s.KubePodDisruptionBudget(
        chart,
        "core-disruption-budget",
        metadata=k8s.ObjectMeta(name=NAME, namespace=NAMESPACE),
        spec=k8s.PodDisruptionBudgetSpec(
            min_available=k8s.IntOrString.from_number(2), selector=k8s.LabelSelector(match_labels=core_selector)
        ),
    )
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
        values={
            "replicas": 3,
            "leaderElection": True,
            "affinity": k8s.Affinity(node_affinity=node_scheduling.zone_affinity(node_scheduling.HOME_AND_OVH_ZONES)),
            "topologySpreadConstraints": [
                node_scheduling.hostname_spread(core_selector, min_domains=None),
                # Three replicas normally cover OVH and both home zones. With one zone left,
                # allow replacements there on different hosts instead of blocking recovery.
                k8s.TopologySpreadConstraint(
                    topology_key=node_scheduling.ZONE_LABEL,
                    max_skew=1,
                    min_domains=1,
                    when_unsatisfiable="DoNotSchedule",
                    label_selector=k8s.LabelSelector(match_labels=core_selector),
                    match_label_keys=["pod-template-hash"],
                    node_taints_policy="Honor",
                ),
            ],
        },
        post_renderers=[
            HelmReleaseSpecPostRenderers(
                kustomize=HelmReleaseSpecPostRenderersKustomize(
                    patches=[
                        HelmReleaseSpecPostRenderersKustomizePatches(
                            target=HelmReleaseSpecPostRenderersKustomizePatchesTarget(kind="Deployment", name=NAME),
                            # Chart 2.4.2 only probes the health port's TCP listener. Crossplane's
                            # HTTP readiness endpoint waits for the actual TLS webhook server.
                            patch=dedent(f"""\
                                apiVersion: apps/v1
                                kind: Deployment
                                metadata:
                                  name: {NAME}
                                  annotations:
                                    {namespaces.VPA_UPDATE_MODE_LABEL}: {Vpa.AUTO}
                                    {VPA_RESOURCE_POLICY_ANNOTATION}: '{core_resource_policy}'
                                spec:
                                  template:
                                    spec:
                                      containers:
                                        - name: crossplane
                                          readinessProbe:
                                            httpGet:
                                              path: /readyz
                                              port: readyz
                                            periodSeconds: 5
                                            timeoutSeconds: 2
                                """),
                        )
                    ]
                )
            )
        ],
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
    chart: Chart,
    directory: RenderedDirectory,
    crossplane: Kustomization,
    external_secrets_operator: Kustomization,
    forgejo_images: Kustomization,
) -> Kustomization:
    """Wait for the LiteLLM provider CRDs and controller before applying managed resources."""
    return flux_kustomization(
        chart,
        LITELLM_PROVIDER_KUSTOMIZATION_NAME,
        directory,
        timeout="15m",
        prune=False,
        wait=True,
        health_check_exprs=[_FORGEJO_PULL_SECRET_HEALTH, PROVIDER_HEALTH],
        depends_on=flux_kustomization_depends_on_many(crossplane, external_secrets_operator, forgejo_images),
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
        health_check_exprs=[PROVIDER_HEALTH],
        depends_on=[flux_kustomization_depends_on(crossplane)],
        description="Install the OpenTofu provider after Crossplane core; its Workspace resources reconcile separately.",
    )
