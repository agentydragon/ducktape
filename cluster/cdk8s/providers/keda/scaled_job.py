"""Ergonomic wrapper for KEDA's `ScaledJob`, following cdk8s-plus's own construction
pattern: a class named after the kind, constructed as `ScaledJob(scope, id, ...)`. Every
keyword is a `ScaledJobSpec` field under its own name and type; `None` leaves it unset,
so KEDA's own default applies. `rollout_strategy` is omitted -- the schema documents it
deprecated in favor of `rollout`'s own `strategy` field.
"""

from __future__ import annotations

from collections.abc import Sequence

from constructs import Construct
from keda_scaledjob_crds.sh.keda import (
    ScaledJob as _ScaledJob,
    ScaledJobSpec,
    ScaledJobSpecJobTargetRef,
    ScaledJobSpecRollout,
    ScaledJobSpecScalingStrategy,
    ScaledJobSpecTriggers,
)

from cluster.cdk8s.metadata import metadata


class ScaledJob(_ScaledJob):
    """KEDA's `ScaledJob`: scales a Kubernetes `Job` template out per unit of external
    work, rather than an HPA-style long-lived Deployment replica count."""

    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        name: str,
        namespace: str,
        job_target_ref: ScaledJobSpecJobTargetRef,
        triggers: Sequence[ScaledJobSpecTriggers],
        labels: dict[str, str] | None = None,
        min_replica_count: int | None = None,
        max_replica_count: int | None = None,
        polling_interval: int | None = None,
        successful_jobs_history_limit: int | None = None,
        failed_jobs_history_limit: int | None = None,
        rollout: ScaledJobSpecRollout | None = None,
        scaling_strategy: ScaledJobSpecScalingStrategy | None = None,
        env_source_container_name: str | None = None,
    ) -> None:
        super().__init__(
            scope,
            id,
            metadata=metadata(name, namespace, labels=labels),
            spec=ScaledJobSpec(
                job_target_ref=job_target_ref,
                triggers=list(triggers),
                min_replica_count=min_replica_count,
                max_replica_count=max_replica_count,
                polling_interval=polling_interval,
                successful_jobs_history_limit=successful_jobs_history_limit,
                failed_jobs_history_limit=failed_jobs_history_limit,
                rollout=rollout,
                scaling_strategy=scaling_strategy,
                env_source_container_name=env_source_container_name,
            ),
        )
