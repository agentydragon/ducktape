"""A warm fixture gets fresh CAs while retaining the real PKI controllers."""

import base64
import json
import os
from pathlib import Path
from typing import Any

import pytest_bazel
import yaml
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from python.runfiles import Runfiles

from agentplane.system_tests.deployment import certificates, rendered_fixture, stack
from agentplane.system_tests.k3s import baseline, pki_infrastructure
from devinfra.testing.undeclared_outputs import undeclared_outputs_dir


def _public_certificate(node: baseline.K3s, name: str) -> x509.Certificate:
    encoded = node.kubectl("-n", "cert-manager", "get", "secret", name, "-o", r"jsonpath={.data.tls\.crt}")
    return x509.load_pem_x509_certificate(base64.b64decode(encoded))


def test_fixture_cas_are_fresh_on_the_same_controllers(tmp_path: Path) -> None:
    runfiles = Runfiles.CreateOrRaise().root()
    template = rendered_fixture.read(runfiles / os.environ["K3S_FIXTURE_RLOCATION"])
    # Keep the production-derived CA hierarchy and trust distribution, with no application workloads.
    objects = [
        doc
        for doc in template.objects
        if doc["kind"] in {"Namespace", "ClusterIssuer", "Bundle"}
        or (
            doc["kind"] == "Certificate"
            and (
                doc["metadata"]["namespace"] == "cert-manager" or doc["metadata"]["name"] == "agentplane-runner-gateway"
            )
        )
    ]
    fixture = template.model_copy(update={"objects": objects})
    cert_manager, trust_manager = pki_infrastructure.releases()
    node = baseline.K3s(tmp_path, runfiles / os.environ["K3S_BINARY_RLOCATION"])
    baseline.stop_previous()
    process = node.start(layers_in_memory=False)
    controllers: dict[str, str] = {}
    evidence: list[dict[str, Any]] = []
    try:
        images = json.loads((runfiles / os.environ["K3S_IMAGE_MANIFEST_RLOCATION"]).read_text())["pki"]
        node.import_images({name: runfiles / path for name, path in images.items()})
        pki_infrastructure.install(node.kubectl, cert_manager, trust_manager)
        for doc in cert_manager + trust_manager:
            if doc["kind"] == "Deployment":
                name = doc["metadata"]["name"]
                controllers[name] = node.kubectl(
                    "-n", "cert-manager", "get", "deployment", name, "-o", "jsonpath={.metadata.uid}"
                )
        # The normal cleanup boundary also verifies that no Sandboxes survived.
        for crd in sorted((runfiles / "ducktape/agentplane/crds/manifests").glob("crd-*.yaml")):
            node.kubectl("apply", "-f", str(crd))
        previous: dict[str, str] = {}
        for cycle in range(2):
            node.kubectl(
                "apply", "-f", "-", input_text=yaml.safe_dump_all(doc for doc in objects if doc["kind"] == "Namespace")
            )
            node.kubectl(
                "apply", "-f", "-", input_text=yaml.safe_dump_all(doc for doc in objects if doc["kind"] != "Namespace")
            )
            certificates.wait_for_issuance(
                node.kubectl, fixture.namespace, ("cluster-internal-ca-bundle", "agentplane-workload-ca-bundle")
            )
            root = _public_certificate(node, "test-runner-root")
            workload = _public_certificate(node, "agentplane-workload-ca")
            workload.verify_directly_issued_by(root)
            revision = int(
                node.kubectl(
                    "-n", "cert-manager", "get", "certificate/test-runner-root", "-o", "jsonpath={.status.revision}"
                )
            )
            # A new issuance must preserve the key that the existing intermediate trusts.
            # Changing duration triggers cert-manager issuance without changing the CA subject.
            node.kubectl(
                "-n", "cert-manager", "patch", "certificate/test-runner-root", "--type=merge",
                "-p", json.dumps({"spec": {"duration": "43800h"}}),
            )  # fmt: skip
            node.kubectl(
                "-n", "cert-manager", "wait", "certificate/test-runner-root",
                f"--for=jsonpath={{.status.revision}}={revision + 1}",
                f"--timeout={pki_infrastructure.READY_SECONDS}s",
            )  # fmt: skip
            renewed_root = _public_certificate(node, "test-runner-root")
            assert renewed_root.serial_number != root.serial_number
            assert renewed_root.public_key().public_bytes(
                serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
            ) == root.public_key().public_bytes(
                serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
            )
            workload.verify_directly_issued_by(renewed_root)
            identities = {
                doc["metadata"]["name"]: node.kubectl(
                    "-n",
                    "cert-manager",
                    "get",
                    "certificate",
                    doc["metadata"]["name"],
                    "-o",
                    "jsonpath={.metadata.uid}",
                )
                for doc in objects
                if doc["kind"] == "Certificate" and doc["metadata"]["namespace"] == "cert-manager"
            }
            if cycle:
                assert all(previous[name] != uid for name, uid in identities.items())
            previous = identities
            evidence.append({"cycle": cycle, "certificate_uids": identities, "chain_valid": True})
            certificates.report(node.kubectl, cert_manager + trust_manager)
            stack._baseline_clean(node, fixture)
            for name, uid in controllers.items():
                assert (
                    node.kubectl("-n", "cert-manager", "get", "deployment", name, "-o", "jsonpath={.metadata.uid}")
                    == uid
                )
    finally:
        output = undeclared_outputs_dir()
        (output / "pki-lifecycle.json").write_text(json.dumps(evidence, indent=2))
        try:
            certificates.report(node.kubectl, cert_manager + trust_manager)
        finally:
            node.stop(process)


if __name__ == "__main__":
    pytest_bazel.main()
