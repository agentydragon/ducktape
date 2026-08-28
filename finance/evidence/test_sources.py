from __future__ import annotations

import pytest_bazel

from finance.evidence.sources import EVIDENCE_SOURCES


def test_output_filenames_unique() -> None:
    filenames = [source.output_filename for source in EVIDENCE_SOURCES]
    assert len(filenames) == len(set(filenames))


if __name__ == "__main__":
    pytest_bazel.main()
