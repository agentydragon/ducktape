"""Builds the Flux `HelmRelease` custom resources the generators install charts with."""

from __future__ import annotations

from collections.abc import Sequence

from constructs import Construct
from flux_helm.io.fluxcd.toolkit.helm import (
    HelmRelease,
    HelmReleaseSpec,
    HelmReleaseSpecChart,
    HelmReleaseSpecChartSpec,
    HelmReleaseSpecChartSpecSourceRef,
    HelmReleaseSpecChartSpecSourceRefKind,
    HelmReleaseSpecDriftDetection,
    HelmReleaseSpecInstall,
    HelmReleaseSpecInstallRemediation,
    HelmReleaseSpecPostRenderers,
    HelmReleaseSpecUpgrade,
    HelmReleaseSpecValuesFrom,
)
from flux_source.io.fluxcd.toolkit.source import HelmRepository

from cluster.cdk8s.metadata import metadata

# Uninstall and retry a failed install three times before the release stalls.
RETRY_FAILED_INSTALL = HelmReleaseSpecInstall(remediation=HelmReleaseSpecInstallRemediation(retries=3))


def helm_repository_source_ref(name: str, namespace: str) -> HelmReleaseSpecChartSpecSourceRef:
    """The `sourceRef` of a `HelmRepository` declared in another chart, by its name and namespace."""
    return HelmReleaseSpecChartSpecSourceRef(
        kind=HelmReleaseSpecChartSpecSourceRefKind.HELM_REPOSITORY, name=name, namespace=namespace
    )


def helm_release(
    scope: Construct,
    name: str,
    namespace: str,
    *,
    repository: HelmRepository | HelmReleaseSpecChartSpecSourceRef,
    chart: str,
    version: str,
    interval: str,
    values: dict[str, object],
    chart_interval: str | None = None,
    timeout: str | None = None,
    install: HelmReleaseSpecInstall | None = None,
    upgrade: HelmReleaseSpecUpgrade | None = None,
    values_from: Sequence[HelmReleaseSpecValuesFrom] | None = None,
    drift_detection: HelmReleaseSpecDriftDetection | None = None,
    post_renderers: Sequence[HelmReleaseSpecPostRenderers] | None = None,
    target_namespace: str | None = None,
    description: str | None = None,
) -> HelmRelease:
    """Add and return a Flux `HelmRelease` of `chart` at `version` from a Helm repository.

    `repository` is the `HelmRepository` construct when it is in the same chart, else
    `helm_repository_source_ref` of the one another chart declares. `chart_interval` is the
    chart template's `spec.chart.spec.interval`; the other keywords are `HelmReleaseSpec`
    fields under the same names and types. `None` leaves a field unset, so Flux's own default
    applies. `description` becomes the `description` annotation (cluster/AGENTS.md).
    """
    match repository:
        case HelmRepository():
            source_ref = helm_repository_source_ref(repository.name, repository.metadata.namespace)
        case HelmReleaseSpecChartSpecSourceRef():
            source_ref = repository
    return HelmRelease(
        scope,
        f"helm-release-{name}",
        metadata=metadata(name, namespace, annotations=None if description is None else {"description": description}),
        spec=HelmReleaseSpec(
            interval=interval,
            timeout=timeout,
            install=install,
            upgrade=upgrade,
            drift_detection=drift_detection,
            target_namespace=target_namespace,
            chart=HelmReleaseSpecChart(
                spec=HelmReleaseSpecChartSpec(
                    chart=chart, version=version, source_ref=source_ref, interval=chart_interval
                )
            ),
            values_from=values_from,
            post_renderers=post_renderers,
            values=values,
        ),
    )
