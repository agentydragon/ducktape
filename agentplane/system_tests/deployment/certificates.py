"""Per-action runner certificate readiness and PKI diagnostics."""

import json
from collections.abc import Callable, Sequence
from typing import Any

from agentplane.system_tests.driver.tracing import tracer
from agentplane.system_tests.k3s.pki_infrastructure import NAMESPACE, READY_SECONDS
from devinfra.testing.undeclared_outputs import undeclared_outputs_dir

Kubectl = Callable[..., str]


def cleanup_fixture_certificates(
    kubectl: Kubectl, objects: Sequence[dict[str, Any]], deleted_namespaces: Sequence[str]
) -> None:
    """Remove fixture Certificates retained outside the deleted workload namespaces."""
    fixture_certificates = []
    for document in objects:
        if document.get("kind") != "Certificate":
            continue
        metadata = document["metadata"]
        namespace = metadata.get("namespace")
        if namespace in deleted_namespaces:
            continue
        if namespace is None:
            raise RuntimeError(f"fixture Certificate {metadata['name']} has no namespace")
        fixture_certificates.append((namespace, metadata["name"], document["spec"]["secretName"]))

    owned_certificates = []
    for namespace, name, secret_name in fixture_certificates:
        certificate = json.loads(kubectl("get", "certificate", name, "-n", namespace, "-o", "json"))
        owned_certificates.append((namespace, name, secret_name, certificate["metadata"]["uid"]))

    for namespace, name, _, _ in owned_certificates:
        kubectl("delete", "certificate", name, "-n", namespace, "--wait=true")
        if kubectl("get", "certificate", name, "-n", namespace, "--ignore-not-found=true", "-o", "name"):
            raise RuntimeError(f"fixture Certificate {namespace}/{name} survived cleanup")

    for namespace in dict.fromkeys(record[0] for record in owned_certificates):
        certificate_uids = {uid for record_namespace, _, _, uid in owned_certificates if record_namespace == namespace}
        requests = json.loads(kubectl("get", "certificaterequests", "-n", namespace, "-o", "json"))["items"]
        owned_requests = [
            request["metadata"]["name"]
            for request in requests
            if any(owner.get("uid") in certificate_uids for owner in request["metadata"].get("ownerReferences", []))
        ]
        if owned_requests:
            kubectl(
                "delete", "certificaterequest", *owned_requests, "-n", namespace,
                "--ignore-not-found=true", "--wait=true",
            )  # fmt: skip
        remaining_requests = json.loads(kubectl("get", "certificaterequests", "-n", namespace, "-o", "json"))["items"]
        if any(
            owner.get("uid") in certificate_uids
            for request in remaining_requests
            for owner in request["metadata"].get("ownerReferences", [])
        ):
            raise RuntimeError(f"fixture CertificateRequests survived cleanup in {namespace}")

    for namespace, _, secret_name, _ in owned_certificates:
        kubectl("delete", "secret", secret_name, "-n", namespace, "--ignore-not-found=true", "--wait=true")
    for namespace, _, secret_name, _ in owned_certificates:
        if kubectl("get", "secret", secret_name, "-n", namespace, "--ignore-not-found=true", "-o", "name"):
            raise RuntimeError(f"fixture Certificate Secret {namespace}/{secret_name} survived cleanup")


def report(kubectl: Kubectl, objects: list[dict[str, Any]]) -> None:
    output = undeclared_outputs_dir() / "certificates"
    output.mkdir(exist_ok=True)
    resources = "pods,certificates,certificaterequests,issuers,clusterissuers,bundles,events"
    (output / "resources.yaml").write_text(kubectl("get", resources, "-A", "-o", "yaml", check=False))
    for doc in objects:
        if doc["kind"] == "Deployment":
            name = doc["metadata"]["name"]
            (output / f"{name}.log").write_text(kubectl(
                "-n", NAMESPACE, "logs", f"deployment/{name}",
                "--all-containers", "--tail=200", check=False,
            ))  # fmt: skip


@tracer.start_as_current_span("system_test.pki.issuance")
def wait_for_issuance(kubectl: Kubectl, namespace: str, bundles: Sequence[str]) -> None:
    kubectl(
        "-n", NAMESPACE, "wait", "--for=condition=Ready", "certificate/agentplane-workload-ca",
        f"--timeout={READY_SECONDS}s",
    )  # fmt: skip
    kubectl(
        "-n", namespace, "wait", "--for=condition=Ready", "certificate/agentplane-runner-gateway",
        f"--timeout={READY_SECONDS}s",
    )  # fmt: skip
    kubectl(
        "wait", "--for=condition=Synced", *(f"bundle/{bundle}" for bundle in bundles), f"--timeout={READY_SECONDS}s"
    )
