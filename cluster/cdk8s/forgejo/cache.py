"""Forgejo's shared cache and queue: an OVH Valkey `RedisReplication`, owned by the `forgejo`
Kustomization."""

from __future__ import annotations

from pathlib import Path

from cdk8s import App, Chart, Size
from cdk8s_plus_34 import Cpu
from redis_operator_redisreplication_crds.in_.opstreelabs.redis.redis import RedisReplicationSpecTolerations

from cluster.cdk8s import node_scheduling
from cluster.cdk8s.generation import write_charts
from cluster.cdk8s.manifest_roots import HAND_WRITTEN_ROOT
from cluster.cdk8s.valkey import valkey_instance

OUTPUT_DIR = f"{HAND_WRITTEN_ROOT}/forgejo/cache"
NAME = "forgejo-valkey-ovh"


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    valkey_instance(
        chart,
        name=NAME,
        namespace="forgejo",
        description=(
            "OVH Valkey for Forgejo cache + queue (shared, HA-ready replacement for per-instance memory cache /"
            " leveldb queue)"
        ),
        memory_request=Size.mebibytes(128),
        cpu_limit=Cpu.millis(500),
        memory_limit=Size.mebibytes(512),
        max_memory_percent_of_limit=80,
        storage_class="local-path-ovh",
        storage_size=Size.gibibytes(2),
        tolerations=[
            RedisReplicationSpecTolerations(
                key=node_scheduling.CONTROL_PLANE_TAINT_KEY, operator="Exists", effect="NoSchedule"
            )
        ],
    )
    return chart


def write_manifests(root: Path) -> None:
    write_charts(root, OUTPUT_DIR, chart)
