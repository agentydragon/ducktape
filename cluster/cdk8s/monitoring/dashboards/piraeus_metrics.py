"""Fixed synthetic Prometheus data for the Piraeus dashboard visual test."""

from util.testing.frontend_visual.context import FROZEN_NOW_MS

END = FROZEN_NOW_MS // 1000
START = END - 24 * 60 * 60


def samples() -> str:
    # Representative nodes and pools, including a node with both media.
    pools = [
        ("storage-a", "hdd", 4 * 2**40, 0.80),
        ("storage-b", "ssd", 2 * 2**40, 0.45),
        ("storage-mixed", "hdd", 2 * 2**40, 0.65),
        ("storage-mixed", "ssd", 1 * 2**40, 0.35),
    ]
    metrics: dict[str, list[tuple[dict[str, str], float]]] = {}

    def add(name: str, labels: dict[str, str], value: float) -> None:
        metrics.setdefault(name, []).append((labels, value))

    for i, (node, pool, capacity, used) in enumerate(pools):
        labels = {"node": node, "storage_pool": pool}
        add("disk_usage:pool_capacity_bytes", labels, capacity)
        add("disk_usage:pool_used_bytes", labels, capacity * used)
        add("disk_usage:pool_free_bytes", labels, capacity * (1 - used))
        add("disk_usage:pool_committed_bytes", labels, capacity * (used + 0.10))
        copy_labels = labels | {
            "phase": "Bound",
            "resource": f"volume-{i}",
            "persistentvolume": f"pv-{i}",
            "namespace": "example",
            "persistentvolumeclaim": f"data-{i}",
        }
        add("disk_usage:volume_allocated_bytes", copy_labels, capacity * used / 2)
        add("disk_usage:volume_capacity_bytes", copy_labels, capacity * 0.55)
        add(
            "disk_usage:pvc_used_bytes",
            {"node": node, "namespace": "example", "persistentvolumeclaim": f"data-{i}"},
            capacity * used / 3,
        )
    for node, tier in (("storage-a", "hdd"), ("storage-b", "ssd"), ("storage-mixed", "hdd")):
        add("kube_node_labels", {"node": node, "label_storage_allegedly_works_tier": tier}, 1)

    lines: list[str] = []
    for name, series in metrics.items():
        lines.append(f"# TYPE {name} gauge")
        for labels, value in series:
            encoded = ",".join(f'{key}="{item}"' for key, item in labels.items())
            lines.extend(f"{name}{{{encoded}}} {value} {timestamp}" for timestamp in range(START, END + 1, 300))
    return "\n".join([*lines, "# EOF", ""])
