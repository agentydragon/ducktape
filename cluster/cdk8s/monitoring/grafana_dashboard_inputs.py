"""Datasource mappings and folders for monitoring's file-backed dashboards."""

from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True)
class FileDashboard:
    # Maps each dashboard input to a datasource name.
    datasources: Mapping[str, str]
    folder: str | None = None


# `dashboards/<name>.json` beside this module, deployed as the `<name>-dashboard` ConfigMap.
FILE_DASHBOARDS = {
    "flux-cluster": FileDashboard(datasources={}, folder="Cluster & Platform"),
    "interface-flap-frequency": FileDashboard(
        datasources={"DS_LOKI": "Loki", "DS_PROMETHEUS": "Mimir"}, folder="Network"
    ),
    "rugged-power": FileDashboard(datasources={"DS_PROMETHEUS": "Mimir"}, folder="Nodes & Hardware"),
    "node-memory": FileDashboard(datasources={"DS_MIMIR": "Mimir"}, folder="Nodes & Hardware"),
    "cluster-storage-io": FileDashboard(datasources={"DS_MIMIR": "Mimir", "DS_LOKI": "Loki"}, folder="Storage"),
    "s3-operations": FileDashboard(datasources={"DS_MIMIR": "Mimir"}, folder="Storage"),
    "cluster-disk-usage": FileDashboard(datasources={"DS_MIMIR": "Mimir"}, folder="Storage"),
    "piraeus-storage": FileDashboard(datasources={"DS_MIMIR": "Mimir"}, folder="Storage"),
    "storage-service-details": FileDashboard(datasources={"DS_MIMIR": "Mimir"}, folder="Storage"),
    "io-pressure": FileDashboard(datasources={"DS_MIMIR": "Mimir"}, folder="Storage"),
    # Beside the AT&T gateway exporter's own "Home gateway" dashboard.
    "home-network-overview": FileDashboard(datasources={"DS_LOKI": "Loki"}, folder="Network"),
}
