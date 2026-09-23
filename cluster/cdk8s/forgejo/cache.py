"""Forgejo's shared cache and queue: an OVH Valkey `RedisReplication`, and the `forgejo-cache`
Flux Kustomization owning it."""

from __future__ import annotations

from pathlib import Path

from cdk8s import App, Chart
from flux_kustomize.io.fluxcd.toolkit.kustomize import KustomizationSpec
from redis_operator_redisreplication_crds.in_.opstreelabs.redis.redis import (
    RedisReplication,
    RedisReplicationSpec,
    RedisReplicationSpecAffinity,
    RedisReplicationSpecAffinityNodeAffinity,
    RedisReplicationSpecAffinityNodeAffinityPreferredDuringSchedulingIgnoredDuringExecution,
    RedisReplicationSpecAffinityNodeAffinityPreferredDuringSchedulingIgnoredDuringExecutionPreference,
    RedisReplicationSpecAffinityNodeAffinityPreferredDuringSchedulingIgnoredDuringExecutionPreferenceMatchExpressions,
    RedisReplicationSpecAffinityNodeAffinityRequiredDuringSchedulingIgnoredDuringExecution,
    RedisReplicationSpecAffinityNodeAffinityRequiredDuringSchedulingIgnoredDuringExecutionNodeSelectorTerms,
    RedisReplicationSpecAffinityNodeAffinityRequiredDuringSchedulingIgnoredDuringExecutionNodeSelectorTermsMatchExpressions,
    RedisReplicationSpecAffinityPodAntiAffinity,
    RedisReplicationSpecAffinityPodAntiAffinityRequiredDuringSchedulingIgnoredDuringExecution,
    RedisReplicationSpecAffinityPodAntiAffinityRequiredDuringSchedulingIgnoredDuringExecutionLabelSelector,
    RedisReplicationSpecKubernetesConfig,
    RedisReplicationSpecKubernetesConfigResources,
    RedisReplicationSpecKubernetesConfigResourcesLimits,
    RedisReplicationSpecKubernetesConfigResourcesRequests,
    RedisReplicationSpecRedisConfig,
    RedisReplicationSpecStorage,
    RedisReplicationSpecStorageVolumeClaimTemplate,
    RedisReplicationSpecStorageVolumeClaimTemplateSpec,
    RedisReplicationSpecStorageVolumeClaimTemplateSpecResources,
    RedisReplicationSpecStorageVolumeClaimTemplateSpecResourcesRequests,
    RedisReplicationSpecTolerations,
)
from source_watcher_crds.io.fluxcd.extensions.source import ArtifactGeneratorSpecArtifacts

from cluster.cdk8s.artifact_generators import artifact_path, artifact_source_ref
from cluster.cdk8s.flux import Kustomization, flux_kustomization, flux_kustomization_depends_on_many
from cluster.cdk8s.generation import write_charts
from cluster.cdk8s.metadata import metadata

OUTPUT_DIR = "cluster/k8s/forgejo/cache"
NAME = "forgejo-valkey-ovh"


def _chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    RedisReplication(
        chart,
        "valkey",
        metadata=metadata(
            NAME,
            "forgejo",
            annotations={
                "description": (
                    "OVH Valkey for Forgejo cache + queue (shared, HA-ready replacement for per-instance memory cache /"
                    " leveldb queue)"
                )
            },
        ),
        spec=RedisReplicationSpec(
            cluster_size=2,
            kubernetes_config=RedisReplicationSpecKubernetesConfig(
                image="valkey/valkey:9-alpine",
                image_pull_policy="IfNotPresent",
                resources=RedisReplicationSpecKubernetesConfigResources(
                    requests={
                        "cpu": RedisReplicationSpecKubernetesConfigResourcesRequests.from_string("50m"),
                        "memory": RedisReplicationSpecKubernetesConfigResourcesRequests.from_string("128Mi"),
                    },
                    limits={
                        "cpu": RedisReplicationSpecKubernetesConfigResourcesLimits.from_string("500m"),
                        "memory": RedisReplicationSpecKubernetesConfigResourcesLimits.from_string("512Mi"),
                    },
                ),
            ),
            redis_config=RedisReplicationSpecRedisConfig(max_memory_percent_of_limit=80),
            storage=RedisReplicationSpecStorage(
                volume_claim_template=RedisReplicationSpecStorageVolumeClaimTemplate(
                    spec=RedisReplicationSpecStorageVolumeClaimTemplateSpec(
                        access_modes=["ReadWriteOnce"],
                        storage_class_name="local-path-ovh",
                        resources=RedisReplicationSpecStorageVolumeClaimTemplateSpecResources(
                            requests={
                                "storage": RedisReplicationSpecStorageVolumeClaimTemplateSpecResourcesRequests.from_string(
                                    "2Gi"
                                )
                            }
                        ),
                    )
                )
            ),
            tolerations=[
                RedisReplicationSpecTolerations(
                    key="node-role.kubernetes.io/control-plane", operator="Exists", effect="NoSchedule"
                )
            ],
            affinity=RedisReplicationSpecAffinity(
                node_affinity=RedisReplicationSpecAffinityNodeAffinity(
                    required_during_scheduling_ignored_during_execution=RedisReplicationSpecAffinityNodeAffinityRequiredDuringSchedulingIgnoredDuringExecution(
                        node_selector_terms=[
                            RedisReplicationSpecAffinityNodeAffinityRequiredDuringSchedulingIgnoredDuringExecutionNodeSelectorTerms(
                                match_expressions=[
                                    RedisReplicationSpecAffinityNodeAffinityRequiredDuringSchedulingIgnoredDuringExecutionNodeSelectorTermsMatchExpressions(
                                        key="topology.kubernetes.io/zone", operator="In", values=["hil-ovh"]
                                    )
                                ]
                            )
                        ]
                    ),
                    # Prefer ordinary workers when this workload tolerates control planes.
                    preferred_during_scheduling_ignored_during_execution=[
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
                ),
                pod_anti_affinity=RedisReplicationSpecAffinityPodAntiAffinity(
                    required_during_scheduling_ignored_during_execution=[
                        RedisReplicationSpecAffinityPodAntiAffinityRequiredDuringSchedulingIgnoredDuringExecution(
                            label_selector=RedisReplicationSpecAffinityPodAntiAffinityRequiredDuringSchedulingIgnoredDuringExecutionLabelSelector(
                                match_labels={"app": NAME}
                            ),
                            topology_key="kubernetes.io/hostname",
                        )
                    ]
                ),
            ),
        ),
    )
    return chart


def write_manifests(root: Path) -> None:
    write_charts(root, OUTPUT_DIR, _chart)


def forgejo_cache(
    chart: Chart, artifact: ArtifactGeneratorSpecArtifacts, valkey: Kustomization, local_path_provisioner: Kustomization
) -> Kustomization:
    name = "forgejo-cache"
    return flux_kustomization(
        chart,
        name,
        spec=KustomizationSpec(
            retry_interval="1m",
            interval="10m",
            timeout="5m",
            source_ref=artifact_source_ref(artifact),
            path=artifact_path(artifact),
            prune=True,
            wait=True,
            # Retry until the forgejo aggregate creates the Namespace. Waiting for
            # Forgejo readiness would deadlock its cache-dependent startup.
            depends_on=flux_kustomization_depends_on_many(valkey, local_path_provisioner),
        ),
    )
