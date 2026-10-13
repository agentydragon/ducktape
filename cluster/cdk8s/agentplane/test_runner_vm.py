"""The guest config the VM SandboxTemplate carries."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any, cast

import pytest
import pytest_bazel
from cdk8s import (
    Chart,
    Testing as Cdk8sTesting,  # not collected by pytest
)
from more_itertools import one

from cluster.cdk8s.agentplane import runner_vm, testing


def _guest_config(*, console_autologin: bool) -> dict[str, Any]:
    chart = Chart(Cdk8sTesting.app(), "test")
    runner_vm.RunnerVmTemplate(
        chart,
        "runner-vm-template",
        replace(testing.ENV, vm_console_autologin=console_autologin),
        labels={},
        state_volume={"claim": {"storageClassName": "test-storage"}},
        platform_instructions="Test platform guidance.",
    )
    config_map = one(doc for doc in cast(list[dict[str, Any]], Cdk8sTesting.synth(chart)) if doc["kind"] == "ConfigMap")
    return cast(dict[str, Any], json.loads(config_map["data"]["config.json"]))


@pytest.mark.parametrize(("console_autologin", "rendered"), [(True, True), (False, None)])
def test_console_autologin_reaches_the_guest_config_only_when_enabled(
    console_autologin: bool, rendered: bool | None
) -> None:
    # An image that predates the field rejects it, so a disabled environment must not render it.
    assert _guest_config(console_autologin=console_autologin).get("console_autologin") == rendered


if __name__ == "__main__":
    pytest_bazel.main()
