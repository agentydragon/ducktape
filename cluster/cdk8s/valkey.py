"""The OT-Container-Kit redis-operator, and `valkey_instance` for the Valkey instances it manages."""

from __future__ import annotations

from collections.abc import Sequence

from cdk8s import ApiObjectMetadata, App, Chart, Size
from cdk8s_plus_34 import Cpu
from constructs import Construct
from flux_helm.io.fluxcd.toolkit.helm import (
    HelmReleaseSpecInstall,
    HelmReleaseSpecInstallCrds,
    HelmReleaseSpecInstallRemediation,
    HelmReleaseSpecUpgrade,
    HelmReleaseSpecUpgradeCrds,
)
from redis_operator_redisreplication_crds.in_.opstreelabs.redis.redis import (
    RedisReplicationSpecAffinityNodeAffinityPreferredDuringSchedulingIgnoredDuringExecution,
    RedisReplicationSpecAffinityNodeAffinityPreferredDuringSchedulingIgnoredDuringExecutionPreference,
    RedisReplicationSpecAffinityNodeAffinityPreferredDuringSchedulingIgnoredDuringExecutionPreferenceMatchExpressions,
    RedisReplicationSpecAffinityNodeAffinityRequiredDuringSchedulingIgnoredDuringExecutionNodeSelectorTermsMatchExpressions,
    RedisReplicationSpecTolerations,
)

from cluster.cdk8s import disruption_budget, namespaces, node_scheduling
from cluster.cdk8s.helm import helm_release, https_helm_repository
from cluster.cdk8s.manifest_roots import GENERATED_ROOT
from cluster.cdk8s.namespaces import Vpa
from cluster.cdk8s.providers.redis_operator.replication import RedisReplication
from cluster.cdk8s.service_ref import Pods

NAME = "valkey"
NAMESPACE = "valkey-system"
OUTPUT_DIR = f"{GENERATED_ROOT}/valkey"
_RELEASE = "redis-operator"
_OPERATOR_TAG = "v0.25.0"


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    namespaces.namespace(
        chart,
        "namespace",
        name=NAMESPACE,
        vpa=Vpa.RECOMMEND,
        annotations={"description": "Redis operator for managing Valkey instances"},
    )
    helm_release(
        chart,
        _RELEASE,
        NAMESPACE,
        repository=https_helm_repository(
            chart, "ot-helm", "flux-system", url="https://ot-container-kit.github.io/helm-charts"
        ),
        chart=_RELEASE,
        version="0.26.1",
        interval="30m",
        install=HelmReleaseSpecInstall(
            crds=HelmReleaseSpecInstallCrds.CREATE_REPLACE, remediation=HelmReleaseSpecInstallRemediation(retries=3)
        ),
        upgrade=HelmReleaseSpecUpgrade(crds=HelmReleaseSpecUpgradeCrds.CREATE_REPLACE),
        values={
            "redisOperator": {"imageTag": _OPERATOR_TAG, "initContainerImageTag": _OPERATOR_TAG},
            # `additionalRedisConfig` reaches our `valkey/valkey:8-alpine` pods only when the init
            # container generates `/etc/redis/redis.conf`, which this gate turns on.
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
    memory_request: Size,
    cpu_limit: Cpu,
    memory_limit: Size,
    max_memory_percent_of_limit: int | None,
    storage_class: str,
    storage_size: Size,
    tolerations: Sequence[RedisReplicationSpecTolerations] | None = None,
) -> RedisReplication:
    """A two-replica Valkey `RedisReplication` in `hil-ovh`, one replica per node, with a PodDisruptionBudget.

    `max_memory_percent_of_limit=None` leaves `maxmemory` unset. `tolerations=None`
    leaves the pod untolerant of any taint.
    """
    replication = RedisReplication(
        scope,
        name,
        metadata=ApiObjectMetadata(name=name, namespace=namespace, annotations={"description": description}),
        image="valkey/valkey:9-alpine",
        cluster_size=2,
        cpu_request=Cpu.millis(50),
        cpu_limit=cpu_limit,
        memory_request=memory_request,
        memory_limit=memory_limit,
        max_memory_percent_of_limit=max_memory_percent_of_limit,
        storage_class=storage_class,
        storage_size=storage_size,
        tolerations=tolerations,
        node_affinity_match=[
            RedisReplicationSpecAffinityNodeAffinityRequiredDuringSchedulingIgnoredDuringExecutionNodeSelectorTermsMatchExpressions(
                key=node_scheduling.ZONE_LABEL, operator="In", values=[node_scheduling.HIL_OVH_ZONE]
            )
        ],
        # Prefer ordinary workers, for an instance that tolerates control planes.
        preferred_node_affinity=[
            RedisReplicationSpecAffinityNodeAffinityPreferredDuringSchedulingIgnoredDuringExecution(
                weight=100,
                preference=RedisReplicationSpecAffinityNodeAffinityPreferredDuringSchedulingIgnoredDuringExecutionPreference(
                    match_expressions=[
                        RedisReplicationSpecAffinityNodeAffinityPreferredDuringSchedulingIgnoredDuringExecutionPreferenceMatchExpressions(
                            key=node_scheduling.CONTROL_PLANE_TAINT_KEY, operator="DoesNotExist"
                        )
                    ]
                ),
            )
        ],
    )
    # The operator labels its Pods `app: <name>`.
    disruption_budget.add_disruption_budget(
        scope, f"{name}-disruption-budget", name=name, pods=Pods(namespace=namespace, labels=(("app", name),))
    )
    return replication
