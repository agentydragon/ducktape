from __future__ import annotations

import pytest_bazel

from util.image_tag import image_provenance


def test_parses_automation_tag() -> None:
    info = image_provenance("devel-20260713014452-83da566")
    assert info.image_tag == "devel-20260713014452-83da566"
    assert info.source_commit == "83da566"
    assert info.source_commit_url == "https://github.com/agentydragon/ducktape/commit/83da566"


def test_non_automation_tag_keeps_tag_without_commit() -> None:
    info = image_provenance("latest")
    assert info.image_tag == "latest"
    assert info.source_commit is None
    assert info.source_commit_url is None


def test_blank_and_absent_tags_mean_absent() -> None:
    assert image_provenance("  ") == image_provenance(None)
    assert image_provenance(None).image_tag is None


def test_explicit_source_commit_wins_over_tag() -> None:
    info = image_provenance("devel-20260713014452-83da566", source_commit="abc1234")
    assert info.source_commit == "abc1234"
    assert info.source_commit_url == "https://github.com/agentydragon/ducktape/commit/abc1234"


def test_blank_source_commit_falls_back_to_tag() -> None:
    assert image_provenance("devel-20260713014452-83da566", source_commit=" ").source_commit == "83da566"


if __name__ == "__main__":
    pytest_bazel.main()
