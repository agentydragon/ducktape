"""`RedisReplication` renders the required fields under their own names, derives pod
anti-affinity from `metadata.name`, and leaves optional CRD fields unset on `None`."""

import pytest_bazel
from cdk8s import ApiObjectMetadata, Chart, Size
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


def _replication(
    *,
    chart: Chart,
    max_memory_percent_of_limit: int | None,
    preferred_node_affinity: list[
        RedisReplicationSpecAffinityNodeAffinityPreferredDuringSchedulingIgnoredDuringExecution
    ]
    | None,
) -> RedisReplication:
    return RedisReplication(
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
        storage_size=Size.gibibytes(1),
        node_affinity_match=_ZONE_MATCH,
        preferred_node_affinity=preferred_node_affinity,
    )


def test_resources_affinity_and_omitted_options(scratch_chart: Chart) -> None:
    """`Size.as_string()` formats in whatever unit it was constructed with -- `memory_limit`
    here is `Size.gibibytes(1)`, which renders as `"1Gi"`, not a forced `"1024Mi"`."""
    replication = _replication(chart=scratch_chart, max_memory_percent_of_limit=None, preferred_node_affinity=None)
    spec = replication.to_json()["spec"]
    resources = spec["kubernetesConfig"]["resources"]
    assert resources["requests"] == {"cpu": "50m", "memory": "64Mi"}
    assert resources["limits"] == {"cpu": "1", "memory": "1Gi"}
    storage_resources = spec["storage"]["volumeClaimTemplate"]["spec"]["resources"]
    assert storage_resources["requests"] == {"storage": "1Gi"}

    pod_anti_affinity = spec["affinity"]["podAntiAffinity"]
    (rule,) = pod_anti_affinity["requiredDuringSchedulingIgnoredDuringExecution"]
    assert rule["labelSelector"]["matchLabels"] == {"app": "test-redis"}
    assert rule["topologyKey"] == "kubernetes.io/hostname"

    assert "redisConfig" not in spec

    assert "preferredDuringSchedulingIgnoredDuringExecution" not in spec["affinity"]["nodeAffinity"]


def test_explicit_memory_and_scheduling_options(scratch_chart: Chart) -> None:
    replication = _replication(
        chart=scratch_chart, max_memory_percent_of_limit=80, preferred_node_affinity=_AVOID_CONTROL_PLANE
    )
    spec = replication.to_json()["spec"]
    assert spec["redisConfig"]["maxMemoryPercentOfLimit"] == 80
    (preferred,) = spec["affinity"]["nodeAffinity"]["preferredDuringSchedulingIgnoredDuringExecution"]
    assert preferred["weight"] == 100
    (expr,) = preferred["preference"]["matchExpressions"]
    assert expr == {"key": "node-role.kubernetes.io/control-plane", "operator": "DoesNotExist"}


if __name__ == "__main__":
    pytest_bazel.main()
