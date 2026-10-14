"""Read per-process DRM allocations without counting duplicate file descriptors."""

from pathlib import Path


def process_drm_memory(pid: int) -> dict:
    clients: dict[str, dict] = {}
    try:
        descriptors = list(Path(f"/proc/{pid}/fdinfo").iterdir())
    except FileNotFoundError, PermissionError:
        return {"pid": pid, "available": False, "clients": []}
    for descriptor in descriptors:
        try:
            fields = dict(line.split(":", 1) for line in descriptor.read_text().splitlines() if ":" in line)
        except FileNotFoundError, PermissionError:
            continue
        client_id = fields.get("drm-client-id", "").strip()
        if not client_id:
            continue
        measurements = {}
        for key, value in fields.items():
            if key.startswith(("drm-resident-", "drm-total-")):
                parts = value.strip().split()
                if len(parts) == 2 and parts[1] == "KiB":
                    measurements[key + "_kib"] = int(parts[0])
        # A process can hold many FDs for one DRM client. Keep its largest
        # observed counters, never sum duplicate descriptors.
        old = clients.setdefault(client_id, {})
        for key, value in measurements.items():
            old[key] = max(old.get(key, 0), value)
    totals = {}
    for field in ("resident-gtt", "total-gtt", "resident-memory", "total-memory"):
        key = f"drm-{field}_kib"
        if any(key in measurements for measurements in clients.values()):
            totals[field.replace("-", "_") + "_kib"] = sum(
                measurements.get(key, 0) for measurements in clients.values()
            )
    return {
        "pid": pid,
        "available": bool(clients),
        "clients": [{"client_id": client_id, **fields} for client_id, fields in sorted(clients.items())],
        **totals,
    }


def ollama_runner_drm_memory(server_pid: int) -> list[dict]:
    try:
        tasks = list(Path(f"/proc/{server_pid}/task").iterdir())
    except FileNotFoundError, PermissionError:
        return []
    children = set()
    for task in tasks:
        try:
            children.update((task / "children").read_text().split())
        except FileNotFoundError, PermissionError:
            continue
    result = []
    for child in sorted(children):
        try:
            if Path(f"/proc/{child}/comm").read_text().strip() == "llama-server":
                result.append(process_drm_memory(int(child)))
        except FileNotFoundError, PermissionError:
            continue
    return result
