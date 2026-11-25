import pytest_bazel
from cdk8s import App
from more_itertools import one

from cluster.cdk8s import etcd
from cluster.scripts.nebula_mesh import Mesh


def test_ready_etcd_targets_match_the_control_plane_roster(scratch_app: App, nebula_roster: Mesh) -> None:
    chart = etcd.chart(scratch_app, nebula_roster)
    endpoint_slice = one(obj for obj in chart.api_objects if obj.kind == "EndpointSlice")
    # Prometheus labels each scrape with the EndpointSlice hostname. Talos serves metrics on the Nebula IP.
    assert sorted(
        (endpoint["hostname"], address)
        for endpoint in endpoint_slice.to_json()["endpoints"]
        if endpoint["conditions"]["ready"]
        for address in endpoint["addresses"]
    ) == sorted((name, host.nebula_ip) for name, host in nebula_roster.control_planes().items())


if __name__ == "__main__":
    pytest_bazel.main()
