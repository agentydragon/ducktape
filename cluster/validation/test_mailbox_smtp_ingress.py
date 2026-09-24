"""Validates the hand-written ingress proxy config against the mailbox SMTP Service it fronts."""

import pytest_bazel
import yaml
from more_itertools import one

from util.bazel.runfiles import get_required_path


def test_ingress_proxy_forwards_to_the_smtp_service_with_proxy_protocol() -> None:
    mailbox_yaml = get_required_path("_main/cluster/k8s/haku/mailbox/haku-mailbox.k8s.yaml")
    ingress_config = get_required_path("_main/cluster/k8s/haku/mailbox/nginx.conf").read_text()
    smtp_service = one(
        doc
        for doc in yaml.safe_load_all(mailbox_yaml.read_text())
        if doc and doc["kind"] == "Service" and doc["metadata"]["name"] == "haku-mailbox-smtp"
    )
    smtp_service_port = one(smtp_service["spec"]["ports"])

    assert "proxy_protocol on;" in ingress_config
    assert (
        f"{smtp_service['metadata']['name']}.{smtp_service['metadata']['namespace']}.svc.cluster.local:"
        f"{smtp_service_port['port']}"
    ) in ingress_config


if __name__ == "__main__":
    pytest_bazel.main()
