from __future__ import annotations

from pathlib import Path


def test_crush_dataset_min_sample_exists(test_data_dir: Path) -> None:
    sample = test_data_dir / "crush_min.jsonl"
    assert sample.stat().st_size > 0, "test sample should not be empty"
