"""Certificate reissue behavior that does not need the integration App."""

from __future__ import annotations

import json
from uuid import uuid4

from tenacity import AsyncRetrying, retry_if_exception_type, stop_after_delay, wait_fixed

from agentplane.system_tests.driver.cluster import Cluster

REQUEST_SECONDS = 30.0


async def cert_manager_reissues_a_deleted_tls_secret(cluster: Cluster) -> None:
    name = f"test-reissue-{uuid4().hex[:8]}"
    await cluster.kubectl(
        "apply",
        "-f",
        "-",
        body={
            "apiVersion": "cert-manager.io/v1",
            "kind": "Certificate",
            "metadata": {"name": name},
            "spec": {
                "secretName": name,
                "issuerRef": {"name": "agentplane-workload", "kind": "ClusterIssuer"},
                # A one-segment service URI: the admission policy lets the manifest applier request it.
                "uris": [f"spiffe://agentplane/{name}"],
                "privateKey": {"algorithm": "ECDSA", "size": 256, "rotationPolicy": "Always"},
                "usages": ["digital signature", "server auth"],
            },
        },
    )
    await cluster.kubectl("wait", "--for=condition=Ready", f"certificate/{name}", f"--timeout={REQUEST_SECONDS}s")
    original = json.loads(await cluster.kubectl("get", "secret", name, "-o", "json"))
    certificate = json.loads(await cluster.kubectl("get", "certificate", name, "-o", "json"))
    revision = certificate["status"]["revision"]
    await cluster.kubectl("delete", "secret", name, "--wait=false")
    async for attempt in AsyncRetrying(
        stop=stop_after_delay(REQUEST_SECONDS),
        wait=wait_fixed(1),
        retry=retry_if_exception_type(AssertionError),
        reraise=True,
    ):
        with attempt:
            encoded = await cluster.kubectl("get", "secret", name, "--ignore-not-found", "-o", "json")
            assert encoded
            replacement = json.loads(encoded)
            assert replacement["metadata"]["uid"] != original["metadata"]["uid"]
            certificate = json.loads(await cluster.kubectl("get", "certificate", name, "-o", "json"))
            assert certificate["status"]["revision"] > revision
            assert any(c["type"] == "Ready" and c["status"] == "True" for c in certificate["status"]["conditions"])
    assert replacement["data"]["tls.crt"] != original["data"]["tls.crt"]
