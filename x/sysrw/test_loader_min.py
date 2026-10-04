from __future__ import annotations

from pathlib import Path

import pytest
import pytest_bazel

from x.sysrw.run_eval import read_dataset
from x.sysrw.schemas import CCRSample, CrushSample


@pytest.mark.parametrize(("filename", "sample_class"), [("ccr_min.jsonl", CCRSample), ("crush_min.jsonl", CrushSample)])
async def test_read_dataset_dispatches_on_request_shape(
    test_data_dir: Path, filename: str, sample_class: type[CCRSample] | type[CrushSample]
):
    ds = await read_dataset(test_data_dir / filename)
    assert [type(s) for s in ds] == [sample_class] * 2


if __name__ == "__main__":
    pytest_bazel.main()
