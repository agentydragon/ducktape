"""Kubernetes resource requirements and sandbox limits."""

from __future__ import annotations

from cdk8s_plus_34 import k8s


def quantities(values: dict[str, str]) -> dict[str, k8s.Quantity]:
    return {key: k8s.Quantity.from_string(value) for key, value in values.items()}


def container_resources(
    *, cpu: str, memory: str, cpu_limit: str | None = None, memory_limit: str
) -> k8s.ResourceRequirements:
    """Requests `cpu` and `memory`; limits `memory`, and `cpu` only when `cpu_limit` is given."""
    limits = {"memory": memory_limit}
    if cpu_limit is not None:
        limits["cpu"] = cpu_limit
    return k8s.ResourceRequirements(requests=quantities({"cpu": cpu, "memory": memory}), limits=quantities(limits))


def sandbox_limits(*, max_pod_cpu: str, max_pod_memory: str) -> k8s.LimitRangeSpec:
    return k8s.LimitRangeSpec(
        limits=[
            k8s.LimitRangeItem(
                type="Container",
                max=quantities({"cpu": "4", "memory": "8Gi"}),
                min=quantities({"cpu": "10m", "memory": "16Mi"}),
                default=quantities({"cpu": "500m", "memory": "512Mi"}),
                default_request=quantities({"cpu": "100m", "memory": "128Mi"}),
            ),
            k8s.LimitRangeItem(type="Pod", max=quantities({"cpu": max_pod_cpu, "memory": max_pod_memory})),
        ]
    )
