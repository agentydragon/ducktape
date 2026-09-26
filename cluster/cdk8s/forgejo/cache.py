"""Forgejo's shared cache and queue: an OVH Valkey `RedisReplication`, and the `forgejo-cache`
Flux Kustomization owning it."""

from __future__ import annotations

from pathlib import Path

from cdk8s import App, Chart, Size
from cdk8s_plus_34 import Cpu
from redis_operator_redisreplication_crds.in_.opstreelabs.redis.redis import RedisReplicationSpecTolerations
from source_watcher_crds.io.fluxcd.extensions.source import ArtifactGeneratorSpecArtifacts

from cluster.cdk8s.flux import Kustomization, flux_kustomization, flux_kustomization_depends_on_many
from cluster.cdk8s.generation import write_charts
from cluster.cdk8s.manifest_roots import GENERATED_ROOT
from cluster.cdk8s.valkey import valkey_instance

OUTPUT_DIR = f"{GENERATED_ROOT}/forgejo/cache"
NAME = "forgejo-valkey-ovh"


def _chart(app: App) -> Chart:
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
                key="node-role.kubernetes.io/control-plane", operator="Exists", effect="NoSchedule"
            )
        ],
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
        artifact,
        timeout="5m",
        # Retry until the forgejo aggregate creates the Namespace. Waiting for
        # Forgejo readiness would deadlock its cache-dependent startup.
        depends_on=flux_kustomization_depends_on_many(valkey, local_path_provisioner),
    )
