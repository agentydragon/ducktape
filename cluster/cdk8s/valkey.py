"""The OT-Container-Kit redis-operator, and `valkey_instance` for the Valkey instances it manages."""

from __future__ import annotations

from pathlib import Path

from cdk8s import App, Chart
from cdk8s_plus_34 import k8s
from constructs import Construct
from flux_helm.io.fluxcd.toolkit.helm import (
    HelmReleaseSpecInstall,
    HelmReleaseSpecInstallCrds,
    HelmReleaseSpecInstallRemediation,
    HelmReleaseSpecUpgrade,
    HelmReleaseSpecUpgradeCrds,
)
from flux_kustomize.io.fluxcd.toolkit.kustomize import KustomizationSpecHealthChecks
from flux_source.io.fluxcd.toolkit.source import HelmRepository, HelmRepositorySpec
from redis_operator_redisreplication_crds.in_.opstreelabs.redis.redis import (
    RedisReplicationSpecAffinityNodeAffinityPreferredDuringSchedulingIgnoredDuringExecution,
    RedisReplicationSpecAffinityNodeAffinityPreferredDuringSchedulingIgnoredDuringExecutionPreference,
    RedisReplicationSpecAffinityNodeAffinityPreferredDuringSchedulingIgnoredDuringExecutionPreferenceMatchExpressions,
    RedisReplicationSpecAffinityNodeAffinityRequiredDuringSchedulingIgnoredDuringExecutionNodeSelectorTermsMatchExpressions,
    RedisReplicationSpecKubernetesConfigResources,
    RedisReplicationSpecKubernetesConfigResourcesLimits,
    RedisReplicationSpecKubernetesConfigResourcesRequests,
)
from source_watcher_crds.io.fluxcd.extensions.source import ArtifactGeneratorSpecArtifacts

from cluster.cdk8s.flux import Kustomization, flux_kustomization
from cluster.cdk8s.generation import write_charts
from cluster.cdk8s.helm import helm_release
from cluster.cdk8s.manifest_roots import GENERATED_ROOT
from cluster.cdk8s.metadata import metadata
from cluster.cdk8s.providers.redis_operator.replication import RedisReplication

NAME = "valkey"
NAMESPACE = "valkey-system"
OUTPUT_DIR = f"{GENERATED_ROOT}/valkey"
_RELEASE = "redis-operator"
_OPERATOR_TAG = "v0.25.0"


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    k8s.KubeNamespace(
        chart,
        "namespace",
        metadata=k8s.ObjectMeta(
            name=NAMESPACE, annotations={"description": "Redis operator for managing Valkey instances"}
        ),
    )
    repository = HelmRepository(
        chart,
        "repository",
        metadata=metadata("ot-helm", "flux-system"),
        spec=HelmRepositorySpec(interval="24h", url="https://ot-container-kit.github.io/helm-charts"),
    )
    helm_release(
        chart,
        _RELEASE,
        NAMESPACE,
        repository=repository,
        chart=_RELEASE,
        version="0.26.1",
        interval="30m",
        install=HelmReleaseSpecInstall(
            crds=HelmReleaseSpecInstallCrds.CREATE_REPLACE, remediation=HelmReleaseSpecInstallRemediation(retries=3)
        ),
        upgrade=HelmReleaseSpecUpgrade(crds=HelmReleaseSpecUpgradeCrds.CREATE_REPLACE),
        values={
            "redisOperator": {"imageTag": _OPERATOR_TAG, "initContainerImageTag": _OPERATOR_TAG},
            "featureGates": {"GenerateConfigInInitContainer": True},
        },
    )
    return chart


def valkey_instance(
    scope: Construct,
    *,
    name: str,
    namespace: str,
    description: str,
    memory_request: str,
    cpu_limit: str,
    memory_limit: str,
    max_memory_percent_of_limit: int | None,
    storage_class: str,
    storage_size: str,
) -> RedisReplication:
    """A two-replica Valkey `RedisReplication` in `hil-ovh`, one replica per node.

    `max_memory_percent_of_limit=None` leaves `maxmemory` unset.
    """
    return RedisReplication(
        scope,
        name,
        metadata=metadata(name, namespace, annotations={"description": description}),
        image="valkey/valkey:9-alpine",
        cluster_size=2,
        resources=RedisReplicationSpecKubernetesConfigResources(
            requests={
                "cpu": RedisReplicationSpecKubernetesConfigResourcesRequests.from_string("50m"),
                "memory": RedisReplicationSpecKubernetesConfigResourcesRequests.from_string(memory_request),
            },
            limits={
                "cpu": RedisReplicationSpecKubernetesConfigResourcesLimits.from_string(cpu_limit),
                "memory": RedisReplicationSpecKubernetesConfigResourcesLimits.from_string(memory_limit),
            },
        ),
        max_memory_percent_of_limit=max_memory_percent_of_limit,
        storage_class=storage_class,
        storage_size=storage_size,
        node_affinity_match=[
            RedisReplicationSpecAffinityNodeAffinityRequiredDuringSchedulingIgnoredDuringExecutionNodeSelectorTermsMatchExpressions(
                key="topology.kubernetes.io/zone", operator="In", values=["hil-ovh"]
            )
        ],
        # Prefer ordinary workers, for an instance that tolerates control planes.
        preferred_node_affinity=[
            RedisReplicationSpecAffinityNodeAffinityPreferredDuringSchedulingIgnoredDuringExecution(
                weight=100,
                preference=RedisReplicationSpecAffinityNodeAffinityPreferredDuringSchedulingIgnoredDuringExecutionPreference(
                    match_expressions=[
                        RedisReplicationSpecAffinityNodeAffinityPreferredDuringSchedulingIgnoredDuringExecutionPreferenceMatchExpressions(
                            key="node-role.kubernetes.io/control-plane", operator="DoesNotExist"
                        )
                    ]
                ),
            )
        ],
    )


def write_manifests(root: Path) -> None:
    write_charts(root, OUTPUT_DIR, chart)


def valkey(chart: Chart, artifact: ArtifactGeneratorSpecArtifacts) -> Kustomization:
    return flux_kustomization(
        chart,
        NAME,
        artifact,
        wait=None,
        health_checks=[
            KustomizationSpecHealthChecks(
                api_version="helm.toolkit.fluxcd.io/v2", kind="HelmRelease", name=_RELEASE, namespace=NAMESPACE
            )
        ],
    )
