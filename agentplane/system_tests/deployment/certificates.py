"""Per-action runner certificate readiness and PKI diagnostics."""

from collections.abc import Callable, Sequence
from typing import Any

from agentplane.system_tests.driver.tracing import tracer
from agentplane.system_tests.k3s.pki_infrastructure import NAMESPACE, READY_SECONDS
from devinfra.testing.undeclared_outputs import undeclared_outputs_dir

Kubectl = Callable[..., str]


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
