"""Ergonomic wrapper for Flux's `HelmRelease`, following cdk8s-plus's own construction pattern:
a class named after the kind, constructed as `HelmRelease(scope, id, ...)`. Every keyword is a
`HelmReleaseSpec` field under its own name and type; `None` leaves it unset, so Flux's own default
applies. No ducktape remediation, interval or source policy lives here -- those are
`cluster.cdk8s.helm.helm_release`'s own.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from cdk8s import ApiObjectMetadata
from constructs import Construct
from flux_helm.io.fluxcd.toolkit.helm import (
    HelmRelease as _HelmRelease,
    HelmReleaseSpec,
    HelmReleaseSpecChart,
    HelmReleaseSpecChartRef,
    HelmReleaseSpecCommonMetadata,
    HelmReleaseSpecDependsOn,
    HelmReleaseSpecDriftDetection,
    HelmReleaseSpecHealthCheckExprs,
    HelmReleaseSpecInstall,
    HelmReleaseSpecKubeConfig,
    HelmReleaseSpecPostRenderers,
    HelmReleaseSpecPostRenderStrategy,
    HelmReleaseSpecRollback,
    HelmReleaseSpecTest,
    HelmReleaseSpecUninstall,
    HelmReleaseSpecUpgrade,
    HelmReleaseSpecValuesFrom,
    HelmReleaseSpecWaitStrategy,
)


class HelmRelease(_HelmRelease):
    """Flux's `HelmRelease`. `interval` is the only field the CRD itself requires; its validation
    takes exactly one of `chart` and `chart_ref`, which raises here instead of at apply."""

    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        metadata: ApiObjectMetadata,
        interval: str,
        chart: HelmReleaseSpecChart | None = None,
        chart_ref: HelmReleaseSpecChartRef | None = None,
        values: Mapping[str, object] | None = None,
        values_from: Sequence[HelmReleaseSpecValuesFrom] | None = None,
        release_name: str | None = None,
        target_namespace: str | None = None,
        storage_namespace: str | None = None,
        service_account_name: str | None = None,
        kube_config: HelmReleaseSpecKubeConfig | None = None,
        depends_on: Sequence[HelmReleaseSpecDependsOn] | None = None,
        timeout: str | None = None,
        max_history: int | None = None,
        persistent_client: bool | None = None,
        suspend: bool | None = None,
        install: HelmReleaseSpecInstall | None = None,
        upgrade: HelmReleaseSpecUpgrade | None = None,
        test: HelmReleaseSpecTest | None = None,
        rollback: HelmReleaseSpecRollback | None = None,
        uninstall: HelmReleaseSpecUninstall | None = None,
        drift_detection: HelmReleaseSpecDriftDetection | None = None,
        wait_strategy: HelmReleaseSpecWaitStrategy | None = None,
        health_check_exprs: Sequence[HelmReleaseSpecHealthCheckExprs] | None = None,
        post_renderers: Sequence[HelmReleaseSpecPostRenderers] | None = None,
        post_render_strategy: HelmReleaseSpecPostRenderStrategy | None = None,
        common_metadata: HelmReleaseSpecCommonMetadata | None = None,
    ) -> None:
        if (chart is None) == (chart_ref is None):
            raise ValueError(f"{metadata.name=}: give exactly one of chart and chart_ref")
        super().__init__(
            scope,
            id,
            metadata=metadata,
            spec=HelmReleaseSpec(
                interval=interval,
                chart=chart,
                chart_ref=chart_ref,
                values=values,
                values_from=list(values_from) if values_from is not None else None,
                release_name=release_name,
                target_namespace=target_namespace,
                storage_namespace=storage_namespace,
                service_account_name=service_account_name,
                kube_config=kube_config,
                depends_on=list(depends_on) if depends_on is not None else None,
                timeout=timeout,
                max_history=max_history,
                persistent_client=persistent_client,
                suspend=suspend,
                install=install,
                upgrade=upgrade,
                test=test,
                rollback=rollback,
                uninstall=uninstall,
                drift_detection=drift_detection,
                wait_strategy=wait_strategy,
                health_check_exprs=list(health_check_exprs) if health_check_exprs is not None else None,
                post_renderers=list(post_renderers) if post_renderers is not None else None,
                post_render_strategy=post_render_strategy,
                common_metadata=common_metadata,
            ),
        )
