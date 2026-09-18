import pytest_bazel
from cdk8s import Testing as Cdk8sTesting  # pytest auto-collects classes named Test*
from more_itertools import one

from cluster.cdk8s.etcd_constructs import TalosEtcdMetrics
from cluster.scripts.nebula_mesh import Host, Mesh


def test_endpoint_slice_lists_the_control_planes_by_nebula_ip() -> None:
    mesh = Mesh(
        hosts={
            "cp-a": Host(nebula_ip="10.42.255.1", role="control-plane", managed_by="tofu-ovh"),
            "worker-b": Host(nebula_ip="10.42.255.2", role="worker", managed_by="tofu-ovh"),
            "cp-c": Host(nebula_ip="10.42.255.3", role="control-plane", managed_by="tofu-ovh"),
        }
    )
    chart = Cdk8sTesting.chart()
    TalosEtcdMetrics(chart, "etcd", mesh)
    endpoint_slice = one(doc for doc in Cdk8sTesting.synth(chart) if doc["kind"] == "EndpointSlice")
    assert endpoint_slice["endpoints"] == [
        {"addresses": ["10.42.255.1"], "conditions": {"ready": True}, "hostname": "cp-a", "nodeName": "cp-a"},
        {"addresses": ["10.42.255.3"], "conditions": {"ready": True}, "hostname": "cp-c", "nodeName": "cp-c"},
    ]


if __name__ == "__main__":
    pytest_bazel.main()
