"""Flux health checks for CloudNativePG Database resources."""

from cnpg_database_crds.io.cnpg.postgresql import Database
from flux_kustomize.io.fluxcd.toolkit.kustomize import KustomizationSpecHealthCheckExprs


def database_health(*, extension: str | None = None) -> KustomizationSpecHealthCheckExprs:
    current = (
        "has(status.applied) && status.applied && "
        "has(status.observedGeneration) && status.observedGeneration == metadata.generation"
    )
    if extension is not None:
        current += f" && has(status.extensions) && status.extensions.exists(e, e.name == {extension!r} && e.applied)"
    return KustomizationSpecHealthCheckExprs(
        api_version=Database.GVK.api_version, kind=Database.GVK.kind, current=current
    )
