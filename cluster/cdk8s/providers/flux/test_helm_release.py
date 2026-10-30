"""`HelmRelease` rejects at construction the `chart`/`chartRef` combinations the CRD's validation
rejects at apply."""

from collections.abc import Callable

import pytest
import pytest_bazel
from cdk8s import ApiObjectMetadata, Chart
from flux_helm.io.fluxcd.toolkit.helm import (
    HelmReleaseSpecChart,
    HelmReleaseSpecChartRef,
    HelmReleaseSpecChartRefKind,
    HelmReleaseSpecChartSpec,
    HelmReleaseSpecChartSpecSourceRef,
    HelmReleaseSpecChartSpecSourceRefKind,
)

from cluster.cdk8s.providers.flux.helm_release import HelmRelease

_CHART = HelmReleaseSpecChart(
    spec=HelmReleaseSpecChartSpec(
        chart="test-chart",
        source_ref=HelmReleaseSpecChartSpecSourceRef(
            kind=HelmReleaseSpecChartSpecSourceRefKind.HELM_REPOSITORY, name="test-repository"
        ),
    )
)
_CHART_REF = HelmReleaseSpecChartRef(kind=HelmReleaseSpecChartRefKind.OCI_REPOSITORY, name="test-chart")


@pytest.mark.parametrize(("chart", "chart_ref"), [(None, None), (_CHART, _CHART_REF)], ids=["neither", "both"])
def test_not_exactly_one_chart_source_raises(
    scratch_chart: Callable[[], Chart], chart: HelmReleaseSpecChart | None, chart_ref: HelmReleaseSpecChartRef | None
) -> None:
    with pytest.raises(ValueError, match="chart_ref"):
        HelmRelease(
            scratch_chart(),
            "release",
            metadata=ApiObjectMetadata(name="test-release", namespace="test-namespace"),
            interval="1h",
            chart=chart,
            chart_ref=chart_ref,
        )


if __name__ == "__main__":
    pytest_bazel.main()
