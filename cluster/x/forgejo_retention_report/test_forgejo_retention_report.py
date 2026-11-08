from datetime import UTC, datetime

import pytest
import pytest_bazel

from cluster.x.forgejo_retention_report.forgejo_retention_report import (
    Protection,
    classify_shape,
    make_report,
    markdown,
)

NOW = datetime(2026, 11, 8, tzinfo=UTC)
OLD = int(datetime(2026, 1, 1, tzinfo=UTC).timestamp())
DIGEST = "sha256:" + "a" * 64


@pytest.mark.parametrize(
    ("version", "shape"),
    [
        ("devel-20261108205121-bb2dec0", "dated-devel"),
        ("main-20261102120000-1234567", "dated-main"),
        ("4141-merge-20260726004042-1234567", "pr-merge"),
        (DIGEST, "digest"),
        ("1234567", "bare-hash"),
        ("latest", "latest"),
        ("_upload", "upload-session"),
        ("v1.61.1-noble", "unrecognized"),
    ],
)
def test_classify_shape(version: str, shape: str) -> None:
    assert classify_shape(version) == shape


def _version(version_id: int, owner: str, name: str, version: str, *, internal: bool = False) -> dict:
    return {
        "id": version_id,
        "owner": owner,
        "name": name,
        "type": "container",
        "version": version,
        "created": OLD,
        "internal": internal,
    }


def test_out_of_policy_groups_report_unrecognized_and_exclusive_bytes() -> None:
    snapshot = {
        "captured_at": "2026-11-08T00:00:00+00:00",
        "versions": [
            _version(1, "ducktape-ci", "img", "devel-20260101000000-1234567"),
            _version(2, "haku", "ui", "main-20260101000000-1234567"),
            _version(3, "haku", "ui", "weird-tag"),
            _version(4, "ducktape-ci", "img", "_upload", internal=True),
        ],
        # blob 10 is held by the in-policy version and the main-* version, blob 11 only by weird-tag.
        "files": [[1, 10, True], [2, 10, True], [3, 11, True], [4, 12, False]],
        "blobs": [
            {"id": 10, "size": 100, "sha256": "0" * 64},
            {"id": 11, "size": 20, "sha256": "1" * 64},
            {"id": 12, "size": 3, "sha256": "2" * 64},
        ],
    }
    report = make_report(snapshot, Protection(), owner="ducktape-ci", keep=0, days=0, now=NOW)

    groups = {(g["owner"], g["shape"]): g for g in report["out_of_policy"]}
    assert set(groups) == {("haku", "dated-main"), ("haku", "unrecognized"), ("ducktape-ci", "upload-session")}
    assert groups["haku", "dated-main"]["exclusive_bytes"] == 0
    assert groups["haku", "dated-main"]["unique_referenced_bytes"] == 100
    assert groups["haku", "unrecognized"]["exclusive_bytes"] == 20
    assert groups["haku", "unrecognized"]["examples"] == ["weird-tag"]
    assert groups["ducktape-ci", "upload-session"]["exclusive_bytes"] == 3
    assert "**UNRECOGNIZED**" in markdown(
        report | {"repos": [], "rollback_days": 0, "workload_counts": {}, "flux_sources": []}
    )


if __name__ == "__main__":
    pytest_bazel.main()
