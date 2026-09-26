"""`RedisReplication` renders the required fields under their own names, derives pod
anti-affinity from `metadata.name`, and leaves optional CRD fields unset on `None`."""

from typing import Any, cast

import pytest_bazel
from cdk8s import ApiObjectMetadata, Size, Testing as Cdk8sTesting
from cdk8s_plus_34 import Cpu
from redis_operator_redisreplication_crds.in_.opstreelabs.redis.redis import (
    RedisReplicationSpecAffinityNodeAffinityPreferredDuringSchedulingIgnoredDuringExecution,
    RedisReplicationSpecAffinityNodeAffinityPreferredDuringSchedulingIgnoredDuringExecutionPreference,
    RedisReplicationSpecAffinityNodeAffinityPreferredDuringSchedulingIgnoredDuringExecutionPreferenceMatchExpressions,
    RedisReplicationSpecAffinityNodeAffinityRequiredDuringSchedulingIgnoredDuringExecutionNodeSelectorTermsMatchExpressions,
)

from cluster.cdk8s.providers.redis_operator.replication import RedisReplication

_ZONE_MATCH = [
    RedisReplicationSpecAffinityNodeAffinityRequiredDuringSchedulingIgnoredDuringExecutionNodeSelectorTermsMatchExpressions(
        key="topology.kubernetes.io/zone", operator="In", values=["test-zone"]
    )
]
_AVOID_CONTROL_PLANE = [
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
]


def _synth(
    *,
    max_memory_percent_of_limit: int | None,
    preferred_node_affinity: list[
        RedisReplicationSpecAffinityNodeAffinityPreferredDuringSchedulingIgnoredDuringExecution
    ]
    | None,
) -> dict[str, Any]:
    chart = Cdk8sTesting.chart()
    RedisReplication(
        chart,
        "instance",
        metadata=ApiObjectMetadata(name="test-redis", namespace="test-namespace"),
        image="valkey/valkey:9-alpine",
        cluster_size=2,
        cpu_request=Cpu.millis(50),
        cpu_limit=Cpu.units(1),
        memory_request=Size.mebibytes(64),
        memory_limit=Size.gibibytes(1),
        max_memory_percent_of_limit=max_memory_percent_of_limit,
        storage_class="test-storage-class",
        storage_size="1Gi",
        node_affinity_match=_ZONE_MATCH,
        preferred_node_affinity=preferred_node_affinity,
    )
    (redis_replication,) = cast(list[dict[str, Any]], Cdk8sTesting.synth(chart))
    return redis_replication


def test_resources_render_as_kubernetes_quantities() -> None:
    redis_replication = _synth(max_memory_percent_of_limit=None, preferred_node_affinity=None)
    resources = redis_replication["spec"]["kubernetesConfig"]["resources"]
    assert resources["requests"] == {"cpu": "50m", "memory": "64Mi"}
    assert resources["limits"] == {"cpu": "1", "memory": "1024Mi"}


def test_pod_anti_affinity_is_derived_from_metadata_name_not_a_parameter() -> None:
    redis_replication = _synth(max_memory_percent_of_limit=None, preferred_node_affinity=None)
    pod_anti_affinity = redis_replication["spec"]["affinity"]["podAntiAffinity"]
    (rule,) = pod_anti_affinity["requiredDuringSchedulingIgnoredDuringExecution"]
    assert rule["labelSelector"]["matchLabels"] == {"app": "test-redis"}
    assert rule["topologyKey"] == "kubernetes.io/hostname"


def test_max_memory_percent_of_limit_none_leaves_redis_config_unset() -> None:
    redis_replication = _synth(max_memory_percent_of_limit=None, preferred_node_affinity=None)
    assert "redisConfig" not in redis_replication["spec"]


def test_max_memory_percent_of_limit_set_configures_redis_config() -> None:
    redis_replication = _synth(max_memory_percent_of_limit=80, preferred_node_affinity=None)
    assert redis_replication["spec"]["redisConfig"]["maxMemoryPercentOfLimit"] == 80


def test_preferred_node_affinity_none_omits_preferred_terms() -> None:
    redis_replication = _synth(max_memory_percent_of_limit=None, preferred_node_affinity=None)
    assert (
        "preferredDuringSchedulingIgnoredDuringExecution" not in redis_replication["spec"]["affinity"]["nodeAffinity"]
    )


def test_preferred_node_affinity_set_is_passed_through() -> None:
    redis_replication = _synth(max_memory_percent_of_limit=None, preferred_node_affinity=_AVOID_CONTROL_PLANE)
    (preferred,) = redis_replication["spec"]["affinity"]["nodeAffinity"][
        "preferredDuringSchedulingIgnoredDuringExecution"
    ]
    assert preferred["weight"] == 100
    (expr,) = preferred["preference"]["matchExpressions"]
    assert expr == {"key": "node-role.kubernetes.io/control-plane", "operator": "DoesNotExist"}


if __name__ == "__main__":
    pytest_bazel.main()
