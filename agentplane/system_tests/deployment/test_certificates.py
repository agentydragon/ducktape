"""Fixture PKI cleanup preserves resources not owned by the fixture."""

import json
from typing import Any

import pytest
import pytest_bazel

from agentplane.system_tests.deployment import certificates


class CertificateResources:
    def __init__(self, *, stuck_requests: frozenset[str] = frozenset()) -> None:
        self.certificates = {
            ("cert-manager", "fixture-root"): {
                "metadata": {"name": "fixture-root", "uid": "root-uid"},
                "spec": {"secretName": "fixture-root-keypair"},
            },
            ("cert-manager", "fixture-workload-ca"): {
                "metadata": {"name": "fixture-workload-ca", "uid": "workload-ca-uid"},
                "spec": {"secretName": "fixture-workload-ca-keypair"},
            },
            ("cert-manager", "retained-controller-certificate"): {
                "metadata": {"name": "retained-controller-certificate", "uid": "controller-uid"},
                "spec": {"secretName": "retained-controller-keypair"},
            },
            ("fixture", "fixture-leaf"): {
                "metadata": {"name": "fixture-leaf", "uid": "leaf-uid"},
                "spec": {"secretName": "fixture-leaf-keypair"},
            },
        }
        self.requests = {
            ("cert-manager", "fixture-root-request"): {
                "metadata": {
                    "name": "fixture-root-request",
                    "ownerReferences": [{"kind": "Certificate", "name": "fixture-root", "uid": "root-uid"}],
                }
            },
            ("cert-manager", "fixture-workload-ca-request"): {
                "metadata": {
                    "name": "fixture-workload-ca-request",
                    "ownerReferences": [
                        {"kind": "Certificate", "name": "fixture-workload-ca", "uid": "workload-ca-uid"}
                    ],
                }
            },
            ("cert-manager", "same-name-unrelated-request"): {
                "metadata": {
                    "name": "same-name-unrelated-request",
                    "ownerReferences": [{"kind": "Certificate", "name": "fixture-root", "uid": "another-root-uid"}],
                }
            },
            ("cert-manager", "retained-controller-request"): {
                "metadata": {
                    "name": "retained-controller-request",
                    "ownerReferences": [
                        {"kind": "Certificate", "name": "retained-controller-certificate", "uid": "controller-uid"}
                    ],
                }
            },
        }
        self.secrets = {
            ("cert-manager", "fixture-root-keypair"),
            ("cert-manager", "fixture-workload-ca-keypair"),
            ("cert-manager", "retained-controller-keypair"),
            ("fixture", "fixture-leaf-keypair"),
        }
        self.stuck_requests = stuck_requests
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, *args: str, **kwargs: object) -> str:
        self.calls.append(args)
        namespace = args[args.index("-n") + 1] if "-n" in args else ""
        if args[0] == "get":
            resource, output = args[1], args[args.index("-o") + 1]
            if resource == "certificaterequests":
                return json.dumps(
                    {
                        "items": [
                            item for (item_namespace, _), item in self.requests.items() if item_namespace == namespace
                        ]
                    }
                )
            name = args[2]
            if resource == "certificate":
                item = self.certificates.get((namespace, name))
                if output == "json":
                    if item is None:
                        raise AssertionError(f"unexpected missing Certificate {namespace}/{name}")
                    return json.dumps(item)
                return f"certificate/{name}" if item is not None else ""
            if resource == "secret":
                return f"secret/{name}" if (namespace, name) in self.secrets else ""
            raise AssertionError(f"unexpected get resource {resource}")

        if args[0] == "delete":
            resource = args[1]
            names = args[2 : args.index("-n")]
            for name in names:
                if resource == "certificate":
                    self.certificates.pop((namespace, name), None)
                elif resource == "certificaterequest":
                    if name not in self.stuck_requests:
                        self.requests.pop((namespace, name), None)
                elif resource == "secret":
                    self.secrets.discard((namespace, name))
                else:
                    raise AssertionError(f"unexpected delete resource {resource}")
            return ""

        raise AssertionError(f"unexpected kubectl command {args}")


def fixture_objects() -> list[dict[str, Any]]:
    return [
        {
            "kind": "Certificate",
            "metadata": {"name": "fixture-root", "namespace": "cert-manager"},
            "spec": {"secretName": "fixture-root-keypair"},
        },
        {
            "kind": "Certificate",
            "metadata": {"name": "fixture-workload-ca", "namespace": "cert-manager"},
            "spec": {"secretName": "fixture-workload-ca-keypair"},
        },
        {
            "kind": "Certificate",
            "metadata": {"name": "fixture-leaf", "namespace": "fixture"},
            "spec": {"secretName": "fixture-leaf-keypair"},
        },
    ]


def test_cleanup_removes_fixture_chain_and_preserves_other_pki_resources() -> None:
    resources = CertificateResources()

    certificates.cleanup_fixture_certificates(resources, fixture_objects(), ["fixture"])

    assert ("cert-manager", "fixture-root") not in resources.certificates
    assert ("cert-manager", "fixture-workload-ca") not in resources.certificates
    assert ("cert-manager", "fixture-root-keypair") not in resources.secrets
    assert ("cert-manager", "fixture-workload-ca-keypair") not in resources.secrets
    assert ("cert-manager", "fixture-root-request") not in resources.requests
    assert ("cert-manager", "fixture-workload-ca-request") not in resources.requests
    assert ("cert-manager", "same-name-unrelated-request") in resources.requests
    assert ("cert-manager", "retained-controller-certificate") in resources.certificates
    assert ("cert-manager", "retained-controller-request") in resources.requests
    assert ("cert-manager", "retained-controller-keypair") in resources.secrets
    assert ("fixture", "fixture-leaf") in resources.certificates
    assert ("fixture", "fixture-leaf-keypair") in resources.secrets
    certificate_deletions = [
        index for index, call in enumerate(resources.calls) if call[:2] == ("delete", "certificate")
    ]
    secret_deletions = [index for index, call in enumerate(resources.calls) if call[:2] == ("delete", "secret")]
    assert max(certificate_deletions) < min(secret_deletions)


def test_cleanup_refuses_to_finish_when_owned_request_survives() -> None:
    resources = CertificateResources(stuck_requests=frozenset({"fixture-root-request"}))

    with pytest.raises(RuntimeError, match="CertificateRequests"):
        certificates.cleanup_fixture_certificates(resources, fixture_objects(), ["fixture"])

    assert ("cert-manager", "fixture-root") not in resources.certificates
    assert ("cert-manager", "fixture-root-request") in resources.requests
    assert ("cert-manager", "fixture-root-keypair") in resources.secrets


if __name__ == "__main__":
    pytest_bazel.main()
