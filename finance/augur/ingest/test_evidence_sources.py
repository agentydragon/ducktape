from __future__ import annotations

from pathlib import Path

import pytest_bazel

from finance.augur.ingest.evidence_sources import EVIDENCE_SOURCES
from util.bazel.runfiles import get_required_path


def _data_dir() -> Path:
    # Resolve via a known file so the test doesn't depend on directory runfiles staging.
    return get_required_path("_main/finance/augur/data/fred_cpi_us.csv").parent


def test_spec_covers_exactly_the_checked_in_evidence_files() -> None:
    # The resolver writes each source to its output_filename and the loader reads it
    # back by that basename, so the spec must mirror exactly the checked-in evidence
    # set — no un-fetched evidence file, no spec entry pointing at a missing file.
    on_disk = {p.name for p in _data_dir().iterdir() if p.suffix in {".csv", ".json"}}
    on_disk.discard("real_history.json")  # fetch_real_history monitoring output, not fitted evidence
    assert {source.output_filename for source in EVIDENCE_SOURCES} == on_disk


def test_output_filenames_unique() -> None:
    filenames = [source.output_filename for source in EVIDENCE_SOURCES]
    assert len(filenames) == len(set(filenames))


def test_provenance_label_is_kind_and_series_id() -> None:
    source = EVIDENCE_SOURCES[0]
    assert source.provenance_label == f"{source.kind}:{source.series_id}"


if __name__ == "__main__":
    pytest_bazel.main()
