import json
import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest_bazel

from devinfra.buildbuddy.ci import vm_shadow
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


def test_cutover_archives_by_executor(tmp_path: Path) -> None:
    env = {
        "PAIR_DIR": str(tmp_path),
        "GITHUB_OUTPUT": str(tmp_path / "output"),
        "GITHUB_RUN_ID": "42",
        "GITHUB_RUN_ATTEMPT": "1",
        "GITHUB_SHA": "abc",
        "REQUIRED_LABEL": "//suites:test_vm",
        "SHADOW_LABEL": "//suites:test_vm_tcg",
        "SHADOW_OUTCOME": "failure",
    }
    with patch.dict(vm_shadow.os.environ, env), patch.object(sys, "argv", ["vm_shadow", "select"]):
        with patch.object(vm_shadow, "_select", return_value="fresh") as select:
            vm_shadow.main()
        record = json.loads((tmp_path / "pair.json").read_text())
        select.assert_called_once_with(record["required"], env["REQUIRED_LABEL"])
        assert record["required"] != record["shadow"]
        assert record["schema_version"] == 2
        assert (record["required_route"], record["shadow_route"]) == ("kvm", "managed")
        with (
            patch.object(sys, "argv", ["vm_shadow", "collect"]),
            patch.object(vm_shadow, "_json", return_value={"invocation": [{"invocationStatus": vm_shadow._COMPLETE}]}),
            patch.object(vm_shadow, "_collect") as collect,
        ):
            vm_shadow.main()
        assert [c.args for c in collect.call_args_list] == [
            (tmp_path / "kvm", record["required"], env["REQUIRED_LABEL"]),
            (tmp_path / "managed", record["shadow"], env["SHADOW_LABEL"]),
        ]


if __name__ == "__main__":
    pytest_bazel.main()
