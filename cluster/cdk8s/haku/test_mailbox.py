"""Mailbox model contracts and Stalwart's init-only admin bootstrap."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
import pytest_bazel
from cdk8s import (
    App,
    Testing as Cdk8sTesting,  # pytest auto-collects classes named Test*
)
from more_itertools import one
from python.runfiles import Runfiles

from cluster.cdk8s.haku import mailbox


@pytest.fixture(scope="module")
def mailbox_manifests(scratch_app_factory: Callable[[], App]) -> list[dict[str, Any]]:
    return Cdk8sTesting.synth(mailbox.chart(scratch_app_factory()))


def test_mailbox_initialization_is_serialized_and_init_only(mailbox_manifests: list[dict[str, Any]]) -> None:
    deployment = one(obj for obj in mailbox_manifests if obj["kind"] == "Deployment")

    assert deployment["spec"]["strategy"]["type"] == "Recreate"
    pod_spec = deployment["spec"]["template"]["spec"]
    initialize_env = {item["name"] for item in one(pod_spec["initContainers"])["env"]}
    production_env = {item["name"] for item in one(pod_spec["containers"])["env"]}
    assert "STALWART_ADMIN_PASSWORD" in initialize_env
    assert "STALWART_ADMIN_PASSWORD" not in production_env
    assert "STALWART_RECOVERY_ADMIN" not in initialize_env | production_env


def test_smtp_ingress_config_targets_the_model_service_with_proxy_protocol(
    mailbox_manifests: list[dict[str, Any]], runfiles: Runfiles
) -> None:
    smtp_service = one(
        obj for obj in mailbox_manifests if obj["kind"] == "Service" and obj["metadata"]["name"] == "smtp"
    )
    smtp_service_port = one(smtp_service["spec"]["ports"])
    ingress_config = (runfiles.root() / "ducktape/cluster/k8s/haku/mailbox/nginx.conf").read_text()

    assert "proxy_protocol on;" in ingress_config
    assert (
        f"{smtp_service['metadata']['name']}.{smtp_service['metadata']['namespace']}.svc.cluster.local:"
        f"{smtp_service_port['port']}"
    ) in ingress_config


if __name__ == "__main__":
    pytest_bazel.main()
