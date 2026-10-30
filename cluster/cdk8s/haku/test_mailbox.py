"""Mailbox model contracts and Stalwart's init-only admin bootstrap."""

from __future__ import annotations

from collections.abc import Callable

import pytest_bazel
from cdk8s import (
    App,
    Testing as Cdk8sTesting,  # pytest auto-collects classes named Test*
)
from more_itertools import one

from cluster.cdk8s.haku import mailbox
from util.bazel.runfiles import get_required_path


def test_mailbox_initialization_is_serialized_and_init_only(scratch_app: Callable[[], App]) -> None:
    deployment = one(obj for obj in Cdk8sTesting.synth(mailbox.chart(scratch_app())) if obj["kind"] == "Deployment")

    assert deployment["spec"]["strategy"]["type"] == "Recreate"
    pod_spec = deployment["spec"]["template"]["spec"]
    initialize_env = {item["name"] for item in one(pod_spec["initContainers"])["env"]}
    production_env = {item["name"] for item in one(pod_spec["containers"])["env"]}
    assert "STALWART_ADMIN_PASSWORD" in initialize_env
    assert "STALWART_ADMIN_PASSWORD" not in production_env
    assert "STALWART_RECOVERY_ADMIN" not in initialize_env | production_env


def test_smtp_ingress_config_targets_the_model_service_with_proxy_protocol(scratch_app: Callable[[], App]) -> None:
    smtp_service = one(
        obj
        for obj in Cdk8sTesting.synth(mailbox.chart(scratch_app()))
        if obj["kind"] == "Service" and obj["metadata"]["name"] == "smtp"
    )
    smtp_service_port = one(smtp_service["spec"]["ports"])
    ingress_config = get_required_path("_main/cluster/k8s/haku/mailbox/nginx.conf").read_text()

    assert "proxy_protocol on;" in ingress_config
    assert (
        f"{smtp_service['metadata']['name']}.{smtp_service['metadata']['namespace']}.svc.cluster.local:"
        f"{smtp_service_port['port']}"
    ) in ingress_config


if __name__ == "__main__":
    pytest_bazel.main()
