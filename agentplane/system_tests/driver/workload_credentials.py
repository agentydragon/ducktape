"""Assert real issuer, WorkloadCredential and Pod bindings without an App client."""

from __future__ import annotations

import base64
import json

from agentplane.environments.v1 import environment_service_pb2
from agentplane.system_tests.driver.cluster import Cluster
from agentplane.system_tests.driver.tracing import tracer

_RUNNER_CREDENTIAL_LABEL = "agentplane.allegedly.works/environment-runtime-credential"
# Issuer labels on the record's Certificate and Secret.
_RECORD_UID_LABEL = "agentplane.allegedly.works/workload-credential-uid"
_CREDENTIAL_ID_LABEL = "agentplane.allegedly.works/workload-credential-id"


async def runner_identity(cluster: Cluster, pod_name: str) -> tuple[str, str]:
    pod = json.loads(await cluster.kubectl("get", "pod", pod_name, "-o", "json"))
    metadata = pod["metadata"]
    labels = metadata["labels"]
    return (metadata["uid"], labels[_RUNNER_CREDENTIAL_LABEL])


async def assert_ready_certificate(cluster: Cluster, sandbox_name: str, credential_id: str, namespace: str) -> None:
    """The Sandbox's WorkloadCredential issued `credential_id`, and cert-manager signed exactly its URI."""
    with tracer.start_as_current_span("system_test.pki.runner_certificate"):
        record = json.loads(await cluster.kubectl("get", "workloadcredential", sandbox_name, "-o", "json"))
        status = record["status"]
        assert status["credentialID"] == credential_id, status
        assert status["uri"] == f"spiffe://agentplane/{namespace}/{sandbox_name}/{credential_id}", status
        assert status["acceptedBinding"] == record["spec"]["binding"], record
        assert any(c["type"] == "Ready" and c["status"] == "True" for c in status["conditions"]), status
        name = status["secretName"]
        certificate = json.loads(await cluster.kubectl("get", "certificate", name, "-o", "json"))
        spec = certificate["spec"]
        assert spec["secretName"] == name
        assert spec["uris"] == [status["uri"]]
        assert set(spec["usages"]) == {"digital signature", "server auth", "client auth"}
        assert any(
            condition["type"] == "Ready" and condition["status"] == "True"
            for condition in certificate["status"]["conditions"]
        )
        secret = json.loads(await cluster.kubectl("get", "secret", name, "-o", "json"))
        assert secret["metadata"]["annotations"]["cert-manager.io/certificate-name"] == name
        assert secret["metadata"]["labels"][_RECORD_UID_LABEL] == record["metadata"]["uid"]
        assert secret["metadata"]["labels"][_CREDENTIAL_ID_LABEL] == credential_id
        requests = json.loads(await cluster.kubectl("get", "certificaterequests", "-o", "json"))["items"]
        issued = [
            request
            for request in requests
            if any(owner["uid"] == certificate["metadata"]["uid"] for owner in request["metadata"]["ownerReferences"])
            and any(
                c["type"] == "Ready" and c["status"] == "True" for c in request.get("status", {}).get("conditions", [])
            )
        ]
        assert len(issued) == 1
        assert any(c["type"] == "Approved" and c["status"] == "True" for c in issued[0]["status"]["conditions"])
        assert base64.b64decode(issued[0]["status"]["certificate"]) == base64.b64decode(secret["data"]["tls.crt"])


async def check_vm_runner_certificate(
    cluster: Cluster, view: environment_service_pb2.EnvironmentDescriptor
) -> tuple[str, str]:
    assert view.kubernetes_diagnostics.HasField("pod")
    identity = await runner_identity(cluster, view.kubernetes_diagnostics.pod.name)
    _, credential_id = identity
    await assert_ready_certificate(cluster, view.target.name, credential_id, view.target.namespace)
    return identity


async def assert_vm_runner_identity_unchanged(cluster: Cluster, pod_name: str, identity: tuple[str, str]) -> None:
    assert await runner_identity(cluster, pod_name) == identity


# gazelle:include_dep @pypi//protobuf
