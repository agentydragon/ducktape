"""Export authored dashboards for browser tests and candidate verification."""

import argparse
from pathlib import Path

from grafana_foundation_sdk.cog.encoder import JSONEncoder
from grafana_foundation_sdk.models import dashboard

from cluster.cdk8s.att_gateway_exporter import dashboard as gateway_dashboard  # Dashboard modules share a name.
from cluster.cdk8s.cdviz import (
    dashboard as ci_dashboard,  # Dashboard modules share a name.
    feedback_dashboard,
)
from cluster.cdk8s.github_exporter import dashboard as github_dashboard  # Dashboard modules share a name.
from cluster.cdk8s.monitoring.dashboards import (
    aiquota_history,
    buildbuddy_executor_pool,
    cluster_disk_usage,
    cluster_storage_io,
    cpap_gateway_disk,
    flux_cluster,
    home_network_overview,
    interface_flap_frequency,
    io_pressure,
    node_memory,
    piraeus_storage,
    rugged_power,
    s3_operations,
    storage_service_details,
    zot_cache_image_pulls,
)


def write_dashboard(destination: Path, definition: dashboard.Dashboard) -> None:
    destination.write_text(JSONEncoder().encode(definition) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output_dir", type=Path)
    directory = parser.parse_args().output_dir
    directory.mkdir(parents=True, exist_ok=True)
    write_dashboard(directory / "buildbuddy-executor-pool.json", buildbuddy_executor_pool.build_dashboard())
    write_dashboard(directory / "zot-cache-image-pulls.json", zot_cache_image_pulls.build_dashboard())
    write_dashboard(directory / "interface-flap-frequency.json", interface_flap_frequency.build_dashboard())
    write_dashboard(directory / "node-memory.json", node_memory.build_dashboard())
    write_dashboard(directory / "cluster-storage-io.json", cluster_storage_io.build_dashboard())
    write_dashboard(directory / "s3-operations.json", s3_operations.build_dashboard())
    write_dashboard(directory / "cluster-disk-usage.json", cluster_disk_usage.build_dashboard())
    write_dashboard(directory / "piraeus-storage.json", piraeus_storage.build_dashboard())
    write_dashboard(directory / "storage-service-details.json", storage_service_details.build_dashboard())
    write_dashboard(directory / "io-pressure.json", io_pressure.build_dashboard())
    write_dashboard(directory / "home-network-overview.json", home_network_overview.build_dashboard())
    write_dashboard(directory / "cpap-gateway-disk.json", cpap_gateway_disk.build_dashboard())
    write_dashboard(directory / "flux-cluster.json", flux_cluster.build_dashboard())
    write_dashboard(directory / "rugged-power.json", rugged_power.build_dashboard())
    write_dashboard(directory / "aiquota-history.json", aiquota_history.build_dashboard())
    write_dashboard(directory / "home-gateway.json", gateway_dashboard.build_dashboard())
    write_dashboard(directory / "github-api-quotas.json", github_dashboard.build_dashboard())
    write_dashboard(directory / "ducktape-ci.json", ci_dashboard.build_dashboard())
    write_dashboard(directory / "ducktape-ci-feedback.json", feedback_dashboard.build_dashboard())


if __name__ == "__main__":
    main()
