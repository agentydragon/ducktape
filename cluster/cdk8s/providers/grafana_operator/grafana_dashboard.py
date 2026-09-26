"""Ergonomic wrapper for grafana-operator's `GrafanaDashboard`. Every real call site
picks the target `Grafana` instance(s) the same way, through a `spec.instanceSelector`
label match, so `instance_selector_labels` builds that nested selector once instead of
at every call site.
"""

from __future__ import annotations

from collections.abc import Sequence

from cdk8s import ApiObjectMetadata
from constructs import Construct
from grafana_grafanadashboard_crds.org.integreatly.grafana import (
    GrafanaDashboard as _GrafanaDashboard,
    GrafanaDashboardSpec,
    GrafanaDashboardSpecConfigMapRef,
    GrafanaDashboardSpecDatasources,
    GrafanaDashboardSpecGrafanaCom,
    GrafanaDashboardSpecInstanceSelector,
)


class GrafanaDashboard(_GrafanaDashboard):
    """`None` leaves a `GrafanaDashboardSpec` field unset, so grafana-operator's own
    default applies. Fields this repo doesn't build yet (`json`, `url`, `plugins`, ...)
    have no keyword here -- add one the day a caller needs it.
    """

    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        metadata: ApiObjectMetadata,
        instance_selector_labels: dict[str, str],
        folder: str | None = None,
        datasources: Sequence[GrafanaDashboardSpecDatasources] | None = None,
        config_map_ref: GrafanaDashboardSpecConfigMapRef | None = None,
        grafana_com: GrafanaDashboardSpecGrafanaCom | None = None,
    ) -> None:
        super().__init__(
            scope,
            id,
            metadata=metadata,
            spec=GrafanaDashboardSpec(
                instance_selector=GrafanaDashboardSpecInstanceSelector(match_labels=instance_selector_labels),
                folder=folder,
                datasources=list(datasources) if datasources is not None else None,
                config_map_ref=config_map_ref,
                grafana_com=grafana_com,
            ),
        )
