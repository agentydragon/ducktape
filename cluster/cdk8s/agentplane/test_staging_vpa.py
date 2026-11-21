"""The static-service VPA bounds stay within their Deployment resource contracts."""

import pytest_bazel
from cdk8s import App
from cdk8s_plus_34 import Deployment
from more_itertools import one

from cluster.cdk8s.agentplane import staging, staging_vpa


def _mib(quantity: str) -> int:
    if quantity.endswith("Gi"):
        return int(quantity[:-2]) * 1024
    assert quantity.endswith("Mi")
    return int(quantity[:-2])


def test_vpa_bounds_fit_staging_deployments(scratch_app: App) -> None:
    deployments = [obj for obj in staging.chart(scratch_app).node.find_all() if isinstance(obj, Deployment)]
    for name, container_name, minimum, maximum in staging_vpa._WORKLOADS:
        deployment = one(deployment for deployment in deployments if deployment.name == name)
        container = one(container for container in deployment.containers if container.name == container_name)
        if minimum is None and maximum is None:
            continue
        resources = container.resources
        assert resources is not None
        memory = resources.memory
        assert memory is not None
        if minimum is not None:
            assert _mib(memory.request.as_string()) <= _mib(minimum)
        if maximum is not None:
            assert memory.limit is not None
            assert _mib(memory.limit.as_string()) >= _mib(maximum)


if __name__ == "__main__":
    pytest_bazel.main()
