"""Ergonomic wrapper for the OT-Container-Kit redis-operator's `RedisReplication`, following
cdk8s-plus's own construction pattern: a class named after the kind, constructed as
`RedisReplication(scope, id, ...)`. No image pin, replica count, topology value, or other
deployment policy lives here -- every field the CRD schema itself defines (image, resources,
storage class/size, cluster size, `redis_config`'s optional maxmemory percent, the required and
preferred node-affinity terms) is a real constructor parameter or, where the CRD schema leaves no
room for variance, a fixed structural choice: PVC access mode (`ReadWriteOnce`, one volume per
replica) and pod anti-affinity spreading replicas by hostname, keyed by the operator's own
`app: <name>` Pod label -- that label is the operator's implementation, not a per-instance choice,
so it is derived from `metadata.name` rather than exposed as a parameter.
"""

from __future__ import annotations

from collections.abc import Sequence

from cdk8s import ApiObjectMetadata, Size
from cdk8s_plus_34 import Cpu
from constructs import Construct
from redis_operator_redisreplication_crds.in_.opstreelabs.redis.redis import (
    RedisReplication as _RedisReplication,
    RedisReplicationSpec,
    RedisReplicationSpecAffinity,
    RedisReplicationSpecAffinityNodeAffinity,
    RedisReplicationSpecAffinityNodeAffinityPreferredDuringSchedulingIgnoredDuringExecution,
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
)

# Replicas spread across nodes by hostname; this is a stable structural choice for the kind, not a
# per-instance one.
_POD_ANTI_AFFINITY_TOPOLOGY_KEY = "kubernetes.io/hostname"


class RedisReplication(_RedisReplication):
    """`storage_class`/`storage_size` are `RedisReplicationSpec.storage`'s PVC template under
    their own names, always `ReadWriteOnce`. `cpu_request`/`cpu_limit`/`memory_request`/
    `memory_limit` build `kubernetes_config.resources` the same way `cdk8s_plus_34.Container`
    builds its own `resources=` internally, so a caller passes `Cpu`/`Size` values instead of
    hand-typing the Kubernetes quantity string. `node_affinity_match` is the CRD's own required
    node-selector match expression(s) (`affinity.nodeAffinity.requiredDuringScheduling...`);
    `preferred_node_affinity` is its optional soft-scheduling terms, passed straight through
    (`None` omits them, so the scheduler applies no preference). `max_memory_percent_of_limit=None`
    leaves `redis_config.maxmemory_percent_of_limit` unset. Every other keyword is a
    `RedisReplicationSpec`/`RedisReplicationSpecKubernetesConfig` field under its own name.
    """

    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        metadata: ApiObjectMetadata,
        image: str,
        cluster_size: int,
        cpu_request: Cpu,
        cpu_limit: Cpu,
        memory_request: Size,
        memory_limit: Size,
        max_memory_percent_of_limit: int | None,
        storage_class: str,
        storage_size: str,
        node_affinity_match: Sequence[
            RedisReplicationSpecAffinityNodeAffinityRequiredDuringSchedulingIgnoredDuringExecutionNodeSelectorTermsMatchExpressions
        ],
        preferred_node_affinity: Sequence[
            RedisReplicationSpecAffinityNodeAffinityPreferredDuringSchedulingIgnoredDuringExecution
        ]
        | None,
    ) -> None:
        if metadata.name is None:
            raise ValueError("RedisReplication requires metadata.name (the pod anti-affinity match label uses it)")
        super().__init__(
            scope,
            id,
            metadata=metadata,
            spec=RedisReplicationSpec(
                cluster_size=cluster_size,
                kubernetes_config=RedisReplicationSpecKubernetesConfig(
                    image=image,
                    image_pull_policy="IfNotPresent",
                    resources=RedisReplicationSpecKubernetesConfigResources(
                        requests={
                            "cpu": RedisReplicationSpecKubernetesConfigResourcesRequests.from_string(
                                cpu_request.amount
                            ),
                            "memory": RedisReplicationSpecKubernetesConfigResourcesRequests.from_string(
                                f"{memory_request.to_mebibytes()}Mi"
                            ),
                        },
                        limits={
                            "cpu": RedisReplicationSpecKubernetesConfigResourcesLimits.from_string(cpu_limit.amount),
                            "memory": RedisReplicationSpecKubernetesConfigResourcesLimits.from_string(
                                f"{memory_limit.to_mebibytes()}Mi"
                            ),
                        },
                    ),
                ),
                redis_config=(
                    None
                    if max_memory_percent_of_limit is None
                    else RedisReplicationSpecRedisConfig(max_memory_percent_of_limit=max_memory_percent_of_limit)
                ),
                storage=RedisReplicationSpecStorage(
                    volume_claim_template=RedisReplicationSpecStorageVolumeClaimTemplate(
                        spec=RedisReplicationSpecStorageVolumeClaimTemplateSpec(
                            access_modes=["ReadWriteOnce"],
                            storage_class_name=storage_class,
                            resources=RedisReplicationSpecStorageVolumeClaimTemplateSpecResources(
                                requests={
                                    "storage": RedisReplicationSpecStorageVolumeClaimTemplateSpecResourcesRequests.from_string(
                                        storage_size
                                    )
                                }
                            ),
                        )
                    )
                ),
                affinity=RedisReplicationSpecAffinity(
                    node_affinity=RedisReplicationSpecAffinityNodeAffinity(
                        required_during_scheduling_ignored_during_execution=RedisReplicationSpecAffinityNodeAffinityRequiredDuringSchedulingIgnoredDuringExecution(
                            node_selector_terms=[
                                RedisReplicationSpecAffinityNodeAffinityRequiredDuringSchedulingIgnoredDuringExecutionNodeSelectorTerms(
                                    match_expressions=list(node_affinity_match)
                                )
                            ]
                        ),
                        preferred_during_scheduling_ignored_during_execution=(
                            None if preferred_node_affinity is None else list(preferred_node_affinity)
                        ),
                    ),
                    pod_anti_affinity=RedisReplicationSpecAffinityPodAntiAffinity(
                        required_during_scheduling_ignored_during_execution=[
                            RedisReplicationSpecAffinityPodAntiAffinityRequiredDuringSchedulingIgnoredDuringExecution(
                                label_selector=RedisReplicationSpecAffinityPodAntiAffinityRequiredDuringSchedulingIgnoredDuringExecutionLabelSelector(
                                    match_labels={"app": metadata.name}
                                ),
                                topology_key=_POD_ANTI_AFFINITY_TOPOLOGY_KEY,
                            )
                        ]
                    ),
                ),
            ),
        )
