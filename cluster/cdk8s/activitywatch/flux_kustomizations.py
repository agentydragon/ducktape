"""Flux Kustomizations for the cluster/k8s/activitywatch slice."""

from __future__ import annotations

from cdk8s import Chart

from cluster.cdk8s.flux import Kustomization, RenderedDirectory, flux_kustomization, flux_kustomization_depends_on_many


def activitywatch(
    chart: Chart, directory: RenderedDirectory, external_secrets_operator: Kustomization
) -> Kustomization:
    name = "activitywatch"
    return flux_kustomization(
        chart,
        name,
        # Revived 2026-08-26: the central aw-server is fed by the repo-owned
        # instance-to-instance importer (@ducktape_activitywatch//importer) over a
        # bearer-gated write route, replacing aw-sync. The cluster Syncthing receiver,
        # importer cronjob, and desktop Syncthing transport are gone.
        directory,
        timeout="5m",
        depends_on=flux_kustomization_depends_on_many(external_secrets_operator),
    )
