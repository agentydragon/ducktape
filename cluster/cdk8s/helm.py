"""Builds the Flux `HelmRepository` and `HelmRelease` custom resources the generators install
charts with."""

from __future__ import annotations

from collections.abc import Sequence

from cdk8s import ApiObjectMetadata
from constructs import Construct
from flux_helm.io.fluxcd.toolkit.helm import (
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
from flux_source.io.fluxcd.toolkit.source import HelmRepositorySpecType

from cluster.cdk8s.providers.flux.git_repository import GitRepository
from cluster.cdk8s.providers.flux.helm_release import HelmRelease
from cluster.cdk8s.providers.flux.helm_repository import HelmRepository

# Uninstall and retry a failed install three times before the release stalls.
RETRY_FAILED_INSTALL = HelmReleaseSpecInstall(remediation=HelmReleaseSpecInstallRemediation(retries=3))


def oci_helm_repository(scope: Construct, name: str, namespace: str, *, url: str) -> HelmRepository:
    """Add and return a Flux OCI `HelmRepository` serving the charts at the `oci://` `url`. It has no
    `interval`: source-controller never polls an OCI repository; each HelmChart pulls on its own."""
    if not url.startswith("oci://"):
        raise ValueError(f"An OCI HelmRepository needs an oci:// URL: {url=}")
    return HelmRepository(
        scope,
        f"helm-repository-{name}",
        metadata=ApiObjectMetadata(name=name, namespace=namespace),
        url=url,
        type=HelmRepositorySpecType.OCI,
    )


def https_helm_repository(
    scope: Construct, name: str, namespace: str, *, url: str, interval: str = "24h"
) -> HelmRepository:
    """Add and return a Flux `HelmRepository` of the default type, serving the chart index at the
    `https://` `url`, which source-controller re-fetches every `interval`. Our policy: 24h."""
    if not url.startswith("https://"):
        raise ValueError(f"A default-type HelmRepository needs an https:// URL here: {url=}")
    return HelmRepository(
        scope,
        f"helm-repository-{name}",
        metadata=ApiObjectMetadata(name=name, namespace=namespace),
        url=url,
        interval=interval,
    )


def helm_repository_source_ref(name: str, namespace: str) -> HelmReleaseSpecChartSpecSourceRef:
    """The `sourceRef` of a `HelmRepository` declared in another chart, by its name and namespace."""
    return HelmReleaseSpecChartSpecSourceRef(
        kind=HelmReleaseSpecChartSpecSourceRefKind.HELM_REPOSITORY, name=name, namespace=namespace
    )


def _source_namespace(source: HelmRepository | GitRepository) -> str:
    if (namespace := source.metadata.namespace) is None:
        raise ValueError(f"{source.kind} has no namespace to reference: {source.name=}")
    return namespace


def helm_release(
    scope: Construct,
    name: str,
    namespace: str,
    *,
    repository: HelmRepository | GitRepository | HelmReleaseSpecChartSpecSourceRef,
    chart: str,
    interval: str,
    values: dict[str, object],
    version: str | None = None,
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
    """Add and return a Flux `HelmRelease` of `chart` from a Helm or Git repository.

    `repository` is the `HelmRepository` or `GitRepository` construct when it is in the same
    chart, else `helm_repository_source_ref` of the one another chart declares. `chart` is a
    chart name in a Helm repository and a path in a Git repository. Our policy: a Helm
    repository's chart is pinned to `version`; a Git repository's chart takes none, its ref pins
    it (Flux ignores `version` there). `chart_interval` is the chart template's
    `spec.chart.spec.interval`; the other keywords are `HelmReleaseSpec` fields under the same
    names and types. `None` leaves a field unset, so Flux's own default applies. `description`
    becomes the `description` annotation (cluster/AGENTS.md).
    """
    match repository:
        case HelmRepository():
            source_ref = helm_repository_source_ref(repository.name, _source_namespace(repository))
        case GitRepository():
            source_ref = HelmReleaseSpecChartSpecSourceRef(
                kind=HelmReleaseSpecChartSpecSourceRefKind.GIT_REPOSITORY,
                name=repository.name,
                namespace=_source_namespace(repository),
            )
        case HelmReleaseSpecChartSpecSourceRef():
            source_ref = repository
    if (version is None) != (source_ref.kind == HelmReleaseSpecChartSpecSourceRefKind.GIT_REPOSITORY):
        raise ValueError(f"{name=}: a Helm repository chart takes a version, a Git repository chart none: {version=}")
    return HelmRelease(
        scope,
        f"helm-release-{name}",
        metadata=ApiObjectMetadata(
            name=name, namespace=namespace, annotations=None if description is None else {"description": description}
        ),
        interval=interval,
        timeout=timeout,
        install=install,
        upgrade=upgrade,
        drift_detection=drift_detection,
        target_namespace=target_namespace,
        chart=HelmReleaseSpecChart(
            spec=HelmReleaseSpecChartSpec(chart=chart, version=version, source_ref=source_ref, interval=chart_interval)
        ),
        values_from=values_from,
        post_renderers=post_renderers,
        values=values,
    )
