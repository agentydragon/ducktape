"""Testing copies the credentials its egress credentials read into the isolated egress namespace."""

from __future__ import annotations

from typing import Any

import pytest_bazel

from cluster.cdk8s.agentplane import testing


def test_every_secret_an_egress_credential_reads_is_copied_in(
    agentplane_manifests: dict[str, list[dict[str, Any]]],
) -> None:
    objects = agentplane_manifests[testing.ENV.namespace]
    read = {
        obj["spec"]["source"]["secretRef"]["name"]
        for obj in objects
        if obj["kind"] == "EgressCredential" and "secretRef" in obj["spec"]["source"]
    }
    copied = {
        obj["spec"]["target"]["name"]
        for obj in objects
        if obj["kind"] == "ExternalSecret" and obj["metadata"]["namespace"] == testing.ENV.egress.credentials_namespace
    }
    assert read
    assert read <= copied


if __name__ == "__main__":
    pytest_bazel.main()
