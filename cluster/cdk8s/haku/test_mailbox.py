"""Mailbox model contracts and Stalwart's init-only admin bootstrap."""

from __future__ import annotations

import pytest_bazel
from cdk8s import App
from cdk8s_plus_34 import k8s
from more_itertools import one
from python.runfiles import Runfiles

from cluster.cdk8s.haku import mailbox


def test_mailbox_initialization_is_serialized_and_init_only(scratch_app: App) -> None:
    chart = mailbox.chart(scratch_app)
    deployment = one(obj for obj in chart.node.find_all() if isinstance(obj, k8s.KubeDeployment))
    spec = deployment.to_json()["spec"]

    assert spec["strategy"]["type"] == "Recreate"
    pod_spec = spec["template"]["spec"]
    initialize_env = {item["name"] for item in one(pod_spec["initContainers"])["env"]}
    production_env = {item["name"] for item in one(pod_spec["containers"])["env"]}
    assert "STALWART_ADMIN_PASSWORD" in initialize_env
    assert "STALWART_ADMIN_PASSWORD" not in production_env
    assert "STALWART_RECOVERY_ADMIN" not in initialize_env | production_env


def test_smtp_ingress_config_targets_the_model_service_with_proxy_protocol(runfiles: Runfiles) -> None:
    ingress_config = (runfiles.root() / "ducktape/cluster/k8s/haku/mailbox/nginx.conf").read_text()

    assert "proxy_protocol on;" in ingress_config
    assert f"server {mailbox.SMTP.authority};" in ingress_config


if __name__ == "__main__":
    pytest_bazel.main()
