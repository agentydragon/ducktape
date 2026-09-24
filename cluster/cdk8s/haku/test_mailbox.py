"""Stalwart's admin bootstrap is serialized and confined to the init container."""

from __future__ import annotations

import pytest_bazel
from cdk8s import Testing as Cdk8sTesting  # pytest auto-collects classes named Test*
from more_itertools import one

from cluster.cdk8s.haku import mailbox


def test_mailbox_initialization_is_serialized_and_init_only() -> None:
    deployment = one(
        obj for obj in Cdk8sTesting.synth(mailbox.chart(Cdk8sTesting.app())) if obj["kind"] == "Deployment"
    )

    assert deployment["spec"]["strategy"]["type"] == "Recreate"
    pod_spec = deployment["spec"]["template"]["spec"]
    initialize_env = {item["name"] for item in one(pod_spec["initContainers"])["env"]}
    production_env = {item["name"] for item in one(pod_spec["containers"])["env"]}
    assert "STALWART_ADMIN_PASSWORD" in initialize_env
    assert "STALWART_ADMIN_PASSWORD" not in production_env
    assert "STALWART_RECOVERY_ADMIN" not in initialize_env | production_env


if __name__ == "__main__":
    pytest_bazel.main()
