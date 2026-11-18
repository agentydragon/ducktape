"""Builds a container's `resources` from the quantities that vary between workloads."""

from __future__ import annotations

from cdk8s_plus_34 import k8s


def container_resources(
    *, cpu: str, memory: str, cpu_limit: str | None = None, memory_limit: str
) -> k8s.ResourceRequirements:
    """Requests `cpu` and `memory`; limits `memory`, and `cpu` only when `cpu_limit` is given."""
    limits = {"memory": k8s.Quantity.from_string(memory_limit)}
    if cpu_limit is not None:
        limits["cpu"] = k8s.Quantity.from_string(cpu_limit)
    return k8s.ResourceRequirements(
        requests={"cpu": k8s.Quantity.from_string(cpu), "memory": k8s.Quantity.from_string(memory)}, limits=limits
    )
