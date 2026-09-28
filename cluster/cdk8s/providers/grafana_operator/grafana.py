"""Ergonomic wrapper for grafana-operator's `Grafana` instance CR, following cdk8s-plus's own
construction pattern: a class named after the kind, constructed as `Grafana(scope, id, *,
metadata, ...)` with `GrafanaSpec` fields under their own names and types.
"""

from __future__ import annotations

from collections.abc import Mapping

from cdk8s import ApiObjectMetadata
from constructs import Construct
from grafana_grafana_crds.org.integreatly.grafana import (
    Grafana as _Grafana,
    GrafanaSpec,
    GrafanaSpecClient,
    GrafanaSpecDeployment,
    GrafanaSpecExternal,
)


class Grafana(_Grafana):
    """grafana-operator's `Grafana` instance. The schema has two real top-level shapes: an
    externally run instance the operator only points at (`external`), and a managed instance
    the operator deploys itself from `config`/`deployment`. `None` leaves a field unset, so
    grafana-operator's own default applies. Fields this repo doesn't build yet (`service`,
    `persistentVolumeClaim`, ...) have no keyword here -- add one the day a caller needs it.
    """

    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        metadata: ApiObjectMetadata,
        external: GrafanaSpecExternal | None = None,
        client: GrafanaSpecClient | None = None,
        config: Mapping[str, Mapping[str, str]] | None = None,
        deployment: GrafanaSpecDeployment | None = None,
    ) -> None:
        super().__init__(
            scope,
            id,
            metadata=metadata,
            spec=GrafanaSpec(external=external, client=client, config=config, deployment=deployment),
        )
