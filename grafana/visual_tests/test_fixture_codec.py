"""Sharing preserves captured values and rejects ambiguous references."""

from copy import deepcopy
from typing import Any

import pytest

from grafana.visual_tests.fixture_codec import compact_fixture, expand_fixture


def test_shared_recording_preserves_gaps_metadata_variants_and_independent_frames() -> None:
    times = [1700000000000, 1700000060000, 1700000300000] * 5
    frame = {
        "schema": {"fields": [{"name": "Time", "type": "time", "config": {"interval": 60000}}]},
        "data": {"values": [times, [None, 0, 1.25, -1, 123456789.5] * 3]},
    }
    fixture: dict[str, Any] = {
        "notes": ["older capture; placeholder retained"],
        "variables": {"host": ["captured-a", "captured-b"]},
        "panels": {
            "1": {
                "A": {
                    "variant_variable": "host",
                    "variants": {"captured-a": {"frames": [frame, deepcopy(frame)]}, "captured-b": {"frames": []}},
                }
            }
        },
    }
    encoded = compact_fixture(fixture)
    assert encoded["shared"]
    assert expand_fixture(encoded) == fixture
    frames = expand_fixture(encoded)["panels"]["1"]["A"]["variants"]["captured-a"]["frames"]
    frames[0]["data"]["values"][0][0] = 0
    assert frames[1] == frame
    assert expand_fixture(encoded) == fixture
    assert compact_fixture(expand_fixture(encoded)) == encoded


def test_ordinary_fixture_is_supported() -> None:
    fixture: dict[str, Any] = {"panels": {"1": {"A": {"frames": []}}}}
    assert expand_fixture(fixture) == fixture


@pytest.mark.parametrize(
    ("shared", "reference"),
    [
        ({}, {"$recorded_ref": "missing"}),
        ({"a": {"$recorded_ref": "a"}}, {"$recorded_ref": "a"}),
        ({"a": {"$recorded_ref": "b"}, "b": {"$recorded_ref": "a"}}, {"$recorded_ref": "a"}),
        ({"a": []}, {"$recorded_ref": "a", "extra": True}),
        ({}, {"$recorded_ref": []}),
    ],
)
def test_invalid_references_fail(shared: dict[str, Any], reference: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match="reference"):
        expand_fixture({"recorded_format": "grafana-recorded-shared-v1", "shared": shared, "fixture": reference})


@pytest.mark.parametrize("fixture", [{"$recorded_ref": "literal"}, {"nested": {"recorded_format": "literal"}}])
def test_reserved_keys_cannot_silently_change_capture(fixture: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match="reserved"):
        compact_fixture(fixture)


def test_unknown_version_fails() -> None:
    with pytest.raises(ValueError, match="format"):
        expand_fixture({"recorded_format": "future"})
