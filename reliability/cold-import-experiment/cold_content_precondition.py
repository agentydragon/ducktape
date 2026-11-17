"""Guard a one-off VM image-import run against reused K3s state."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from agentplane.system_tests.k3s import baseline


def pytest_configure(config: pytest.Config) -> None:
    del config
    baseline_root = baseline._BASELINE_ROOT
    marker = baseline._BASELINE_MARKER
    data_dir = baseline._data_directory()
    root_entries = sorted(path.name for path in baseline_root.iterdir()) if baseline_root.exists() else []
    data_dir_exists = data_dir.exists()
    if marker.exists() or root_entries or data_dir_exists:
        raise pytest.UsageError(
            "cold-content precondition failed before collection: "
            f"baseline_root={baseline_root} entries={root_entries} marker_exists={marker.exists()} "
            f"data_dir={data_dir} data_dir_exists={data_dir_exists}"
        )

    output_dir = Path(os.environ["TEST_UNDECLARED_OUTPUTS_DIR"])
    output_dir.mkdir(parents=True, exist_ok=True)
    proof = {
        "baseline_root": str(baseline_root),
        "baseline_root_exists": baseline_root.exists(),
        "baseline_root_entries": root_entries,
        "baseline_marker": str(marker),
        "baseline_marker_exists": marker.exists(),
        "data_dir": str(data_dir),
        "data_dir_exists": data_dir_exists,
        "containerd_store": str(data_dir / "agent/containerd"),
        "containerd_store_exists": (data_dir / "agent/containerd").exists(),
        "assertion": "baseline marker/root and unique K3s data directory were absent before pytest collection",
    }
    (output_dir / "cold-content-preconditions.json").write_text(json.dumps(proof, indent=2) + "\n")
