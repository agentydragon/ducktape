from __future__ import annotations

import pytest_bazel

from finance.augur.ingest.evidence_sources import EVIDENCE_SOURCES


def test_output_filenames_unique() -> None:
    filenames = [source.output_filename for source in EVIDENCE_SOURCES]
    assert len(filenames) == len(set(filenames))


def test_provenance_label_is_kind_and_series_id() -> None:
    source = EVIDENCE_SOURCES[0]
    assert source.provenance_label == f"{source.kind}:{source.series_id}"


if __name__ == "__main__":
    pytest_bazel.main()
