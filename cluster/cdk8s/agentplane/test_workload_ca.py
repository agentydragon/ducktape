"""Staging's SVIDs come from the workload CA, and every SPIFFE verifier trusts that CA alone."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import PurePosixPath
from typing import Any

import pytest_bazel
import yaml
from more_itertools import one

from cluster.cdk8s.agentplane import egress, workload_ca
from cluster.cdk8s.cert_manager.cluster_ca import INTERNAL_ISSUER

Body = dict[str, Any]


def _resource(documents: list[Body], kind: str, name: str) -> Body:
    return one(doc for doc in documents if doc["kind"] == kind and doc["metadata"]["name"] == name)


def _spiffe_ca_files(value: object) -> Iterator[str]:
    """Every `ca_file` of a SPIFFE authentication entry anywhere in a parsed settings document."""
    if isinstance(value, dict):
        if value.get("mode") == "spiffe":
            yield value["ca_file"]
        for child in value.values():
            yield from _spiffe_ca_files(child)
    elif isinstance(value, list):
        for child in value:
            yield from _spiffe_ca_files(child)


def _spiffe_trust(documents: list[Body]) -> dict[str, set[str]]:
    """Per Deployment, the ConfigMap each SPIFFE verifier's `ca_file` resolves to through its mounts."""
    config_maps = {
        (doc["metadata"]["namespace"], doc["metadata"]["name"]): doc for doc in documents if doc["kind"] == "ConfigMap"
    }
    trust: dict[str, set[str]] = {}
    for deployment in (doc for doc in documents if doc["kind"] == "Deployment"):
        namespace = deployment["metadata"]["namespace"]
        pod = deployment["spec"]["template"]["spec"]
        volumes = {volume["name"]: volume for volume in pod.get("volumes", [])}
        for container in pod["containers"]:
            mounts = {PurePosixPath(m["mountPath"]): volumes[m["name"]] for m in container.get("volumeMounts", [])}
            for volume in mounts.values():
                settings = config_maps.get((namespace, volume.get("configMap", {}).get("name")))
                if settings is None:
                    continue
                for key, content in settings.get("data", {}).items():
                    if not key.endswith((".yaml", ".yml", ".json")):
                        continue
                    for ca_file in _spiffe_ca_files(yaml.safe_load(content)):
                        mounted = mounts[PurePosixPath(ca_file).parent]
                        trust.setdefault(deployment["metadata"]["name"], set()).add(mounted["configMap"]["name"])
    return trust


def test_every_spiffe_verifier_trusts_only_the_workload_ca(staging_manifests: list[Body]) -> None:
    trust = _spiffe_trust(staging_manifests)
    # The egress proxy's forward listener and the services' listeners, through `workload_tls`.
    assert {egress.NAME, "actions", "llm-ingress"} <= trust.keys()
    assert {name: bundles for name, bundles in trust.items() if bundles != {workload_ca.BUNDLE}} == {}


def test_agentplane_svids_come_from_the_workload_ca(staging_manifests: list[Body]) -> None:
    svids = [
        doc
        for doc in staging_manifests
        if doc["kind"] == "Certificate"
        and any(uri.startswith("spiffe://agentplane/") for uri in doc["spec"].get("uris", []))
    ]
    assert svids
    assert {
        doc["metadata"]["name"]
        for doc in svids
        if doc["spec"]["issuerRef"] != {"name": workload_ca.ISSUER.name, "kind": workload_ca.ISSUER.kind}
    } == set()


def test_the_workload_trust_bundle_carries_the_issuers_ca_where_verifiers_run(staging_manifests: list[Body]) -> None:
    issuer = _resource(staging_manifests, "ClusterIssuer", workload_ca.ISSUER.name)
    ca_secret = issuer["spec"]["ca"]["secretName"]
    ca = one(
        doc for doc in staging_manifests if doc["kind"] == "Certificate" and doc["spec"]["secretName"] == ca_secret
    )
    # A ClusterIssuer reads its CA from cert-manager's own namespace.
    assert ca["metadata"]["namespace"] == "cert-manager"
    assert ca["spec"]["isCA"] is True
    # Chained to the cluster root, so clients verifying server certificates with the cluster bundle accept its leaves.
    assert ca["spec"]["issuerRef"] == {"name": INTERNAL_ISSUER.name, "kind": INTERNAL_ISSUER.kind}

    bundle = _resource(staging_manifests, "Bundle", workload_ca.BUNDLE)
    assert bundle["spec"]["sources"] == [{"secret": {"name": ca_secret, "key": "tls.crt"}}]
    selected = one(bundle["spec"]["target"]["namespaceSelector"]["matchExpressions"])
    assert selected["key"] == "kubernetes.io/metadata.name"
    assert selected["operator"] == "In"
    mounted_in = {
        deployment["metadata"]["namespace"]
        for deployment in staging_manifests
        if deployment["kind"] == "Deployment"
        for volume in deployment["spec"]["template"]["spec"].get("volumes", [])
        if volume.get("configMap", {}).get("name") == workload_ca.BUNDLE
    }
    assert mounted_in
    assert mounted_in <= set(selected["values"])
    assert bundle["spec"]["target"]["configMap"]["key"] == workload_ca.BUNDLE_KEY


if __name__ == "__main__":
    pytest_bazel.main()
