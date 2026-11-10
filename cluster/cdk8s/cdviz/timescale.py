"""CNPG's pinned PostgreSQL 18 operand and TimescaleDB extension image for CDviz."""

from cnpg_cluster_crds.io.cnpg.postgresql import (
    ClusterSpecPostgresql,
    ClusterSpecPostgresqlExtensions,
    ClusterSpecPostgresqlExtensionsImage,
)

POSTGRES_IMAGE = "ghcr.io/cloudnative-pg/postgresql:18.6-minimal-trixie@sha256:e5fffed938c8b55b41d9a6a88a47424d46f90aa15d807aa60348b85ea5bd498f"
TIMESCALEDB_OSS_IMAGE = "ghcr.io/cloudnative-pg/timescaledb-oss:2.31.0-18-trixie@sha256:74f87f5d049df4a302c32240e741fc0aff5bf3a3a9cbe84f2959d37e5b24f1f2"


def postgresql() -> ClusterSpecPostgresql:
    """CNPG's TimescaleDB OSS image volume and settings from the upstream recipe."""
    return ClusterSpecPostgresql(
        extensions=[
            ClusterSpecPostgresqlExtensions(
                name="timescaledb-oss", image=ClusterSpecPostgresqlExtensionsImage(reference=TIMESCALEDB_OSS_IMAGE)
            )
        ],
        shared_preload_libraries=["timescaledb"],
        parameters={"max_locks_per_transaction": "128", "timescaledb.telemetry_level": "off"},
    )
