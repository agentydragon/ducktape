"""Ergonomic wrapper for grafana-operator's `GrafanaDatasource`, following cdk8s-plus's own
construction pattern: a class named after the kind, constructed as `GrafanaDatasource(scope,
id, *, metadata, ...)` with `GrafanaDatasourceSpec` fields under their own names and types.
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
        instance_selector: GrafanaDatasourceSpecInstanceSelector,
        datasource: GrafanaDatasourceSpecDatasource,
        values_from: Sequence[GrafanaDatasourceSpecValuesFrom] | None = None,
    ) -> None:
        super().__init__(
            scope,
            id,
            metadata=metadata,
            spec=GrafanaDatasourceSpec(
                instance_selector=instance_selector,
                datasource=datasource,
                values_from=list(values_from) if values_from is not None else None,
            ),
        )
