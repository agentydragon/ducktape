"""Validates the inbound-mail ingress that fronts the `mx.allegedly.works` record's nodes."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest_bazel
import yaml
from more_itertools import one

from util.bazel.runfiles import get_required_path


def _documents(path: Path) -> list[dict[str, Any]]:
    return [doc for doc in yaml.safe_load_all(path.read_text()) if doc]


def _resource(path: Path, kind: str, name: str) -> dict[str, Any]:
    return one(doc for doc in _documents(path) if doc["kind"] == kind and doc["metadata"]["name"] == name)


def test_mailbox_smtp_ingress_covers_public_kubernetes_nodes() -> None:
    """Every public MX node should run a source-preserving port-25 proxy."""
    service_yaml = get_required_path("_main/cluster/k8s/haku/mailbox/app/service.yaml")
    ingress_yaml = get_required_path("_main/cluster/k8s/haku/mailbox/app/smtp-ingress.yaml")
    ingress_config = get_required_path("_main/cluster/k8s/haku/mailbox/app/nginx.conf").read_text()
    namespace_yaml = get_required_path("_main/cluster/k8s/haku/mailbox-namespace/namespace.yaml")

    smtp_service = _resource(service_yaml, "Service", "haku-mailbox-smtp")
    assert "externalIPs" not in smtp_service["spec"]
    smtp_service_port = one(smtp_service["spec"]["ports"])

    daemonset = _resource(ingress_yaml, "DaemonSet", "haku-mailbox-smtp-ingress")
    pod_spec = daemonset["spec"]["template"]["spec"]
    # SMTP serves the public MX nodes; Gateway also runs on internal clients.
    assert pod_spec["nodeSelector"] == {"topology.kubernetes.io/region": "hil"}
    smtp_container = one(container for container in pod_spec["containers"] if container["name"] == "nginx")
    smtp_port = one(port for port in smtp_container["ports"] if port["name"] == "smtp")
    assert smtp_service_port["targetPort"] == smtp_port["name"]
    assert smtp_service_port["port"] == smtp_port["containerPort"]
    service_protocol = smtp_service_port.get("protocol", "TCP")
    assert service_protocol == smtp_port.get("protocol", "TCP")
    namespace = _resource(namespace_yaml, "Namespace", "haku-mailbox")
    assert namespace["metadata"]["labels"]["pod-security.kubernetes.io/enforce"] == "privileged"

    assert "proxy_protocol on;" in ingress_config
    assert (
        f"{smtp_service['metadata']['name']}.{smtp_service['metadata']['namespace']}.svc.cluster.local:"
        f"{smtp_service_port['port']}"
    ) in ingress_config

    ingress_policy = _resource(ingress_yaml, "CiliumNetworkPolicy", "haku-mailbox-smtp-ingress")
    ingress_rule = one(ingress_policy["spec"]["ingress"])
    assert set(ingress_rule["fromEntities"]) == {"world", "host"}
    ingress_port = one(one(ingress_rule["toPorts"])["ports"])
    assert (int(ingress_port["port"]), ingress_port["protocol"]) == (smtp_service_port["port"], service_protocol)

    mailbox_policy = _resource(ingress_yaml, "CiliumNetworkPolicy", "haku-mailbox")
    ingress_labels = daemonset["spec"]["template"]["metadata"]["labels"]
    smtp_rule = one(
        rule
        for rule in mailbox_policy["spec"]["ingress"]
        if len(rule.get("fromEndpoints", [])) == 1
        and rule["fromEndpoints"][0]["matchLabels"].items() <= ingress_labels.items()
    )
    mailbox_port = one(one(smtp_rule["toPorts"])["ports"])
    assert (int(mailbox_port["port"]), mailbox_port["protocol"]) == (smtp_service_port["port"], service_protocol)


if __name__ == "__main__":
    pytest_bazel.main()
