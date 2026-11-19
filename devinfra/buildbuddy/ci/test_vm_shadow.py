from typing import Any

import pytest_bazel

from devinfra.buildbuddy.ci.vm_shadow import first_attempt


def test_second_attempt_cannot_select_or_hide_first_attempt() -> None:
    cached = {"id": {"testResult": {"attempt": 1}}, "testResult": {"cachedLocally": True}}
    fresh = {"id": {"testResult": {"attempt": 2}}, "testResult": {"executionInfo": {"strategy": "remote"}}}
    assert first_attempt({"testResultEvents": [fresh, cached]}) == "cached"
    assert first_attempt({"testResultEvents": [fresh]}) is None


def test_fresh_failure_selects_but_remote_cache_hit_does_not() -> None:
    result: dict[str, Any] = {
        "id": {"testResult": {"attempt": 1}},
        "testResult": {"status": "FAILED", "executionInfo": {"strategy": "remote"}},
    }
    assert first_attempt({"testResultEvents": [result]}) == "fresh"
    result["testResult"]["executionInfo"] = {"cachedRemotely": True, "strategy": "remote"}
    assert first_attempt({"testResultEvents": [result]}) == "cached"


if __name__ == "__main__":
    pytest_bazel.main()
