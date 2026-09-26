"""Ergonomic wrapper for grafana-operator's `GrafanaDatasource`. Like `GrafanaDashboard`,
every real call site picks the target `Grafana` instance(s) through the same
`spec.instanceSelector` label match.
"""

from __future__ import annotations

from collections.abc import Sequence

from cdk8s import ApiObjectMetadata
from constructs import Construct
from grafana_grafanadatasource_crds.org.integreatly.grafana import (
    GrafanaDatasource as _GrafanaDatasource,
    GrafanaDatasourceSpec,
    GrafanaDatasourceSpecDatasource,
    GrafanaDatasourceSpecInstanceSelector,
    GrafanaDatasourceSpecValuesFrom,
)


class GrafanaDatasource(_GrafanaDatasource):
    """`None` leaves a `GrafanaDatasourceSpec` field unset, so grafana-operator's own
    default applies."""

    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        metadata: ApiObjectMetadata,
        instance_selector_labels: dict[str, str],
        datasource: GrafanaDatasourceSpecDatasource,
        values_from: Sequence[GrafanaDatasourceSpecValuesFrom] | None = None,
    ) -> None:
        super().__init__(
            scope,
            id,
            metadata=metadata,
            spec=GrafanaDatasourceSpec(
                instance_selector=GrafanaDatasourceSpecInstanceSelector(match_labels=instance_selector_labels),
                datasource=datasource,
                values_from=list(values_from) if values_from is not None else None,
            ),
        )
