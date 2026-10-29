"""Real cert-manager issuance and trust-manager distribution in the bare-k3s stack."""

import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml
from tenacity import Retrying, retry_if_exception_type, stop_after_delay, wait_fixed

from util.bazel.runfiles import get_required_path
from util.testing.undeclared_outputs import undeclared_outputs_dir

Kubectl = Callable[..., str]
NAMESPACE = "cert-manager"
READY_SECONDS = 180
_LAYOUTS = "_main/agentplane/system_tests/k3s/pki"


def _manifest(variable: str) -> list[dict[str, Any]]:
    return [doc for doc in yaml.safe_load_all(get_required_path(os.environ[variable]).read_text()) if doc]


def releases() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    return _manifest("CERT_MANAGER_INSTALL_RLOCATION"), _manifest("TRUST_MANAGER_INSTALL_RLOCATION")


def images(objects: list[dict[str, Any]]) -> dict[str, Path]:
    references = {
        container["image"]
        for doc in objects
        if doc["kind"] == "Deployment"
        for container in doc["spec"]["template"]["spec"].get("initContainers", [])
        + doc["spec"]["template"]["spec"]["containers"]
    }
    return {
        reference: get_required_path(
            f"{_LAYOUTS}/{reference.rsplit('/', 1)[1].split(':')[0].replace('-', '_')}_layout.rloc"
        )
        for reference in references
    }


def _wait_for_api(kubectl: Kubectl, resource: dict[str, Any]) -> None:
    # Available Pods precede CA injection into webhook configurations. Exercise admission itself.
    for attempt in Retrying(
        stop=stop_after_delay(READY_SECONDS),
        wait=wait_fixed(1),
        retry=retry_if_exception_type(RuntimeError),
        reraise=True,
    ):
        with attempt:
            kubectl("create", "--dry-run=server", "-f", "-", input_text=yaml.safe_dump(resource))


def _install(kubectl: Kubectl, objects: list[dict[str, Any]], probe: dict[str, Any]) -> None:
    kubectl("apply", "--server-side", "-f", "-", input_text=yaml.safe_dump_all(objects))
    crds = [f"crd/{doc['metadata']['name']}" for doc in objects if doc["kind"] == "CustomResourceDefinition"]
    kubectl("wait", "--for=condition=Established", *crds, f"--timeout={READY_SECONDS}s")
    for doc in objects:
        if doc["kind"] == "Deployment":
            kubectl(
                "-n", NAMESPACE, "rollout", "status", f"deployment/{doc['metadata']['name']}",
                f"--timeout={READY_SECONDS}s",
            )  # fmt: skip
    _wait_for_api(kubectl, probe)


def install(kubectl: Kubectl, cert_manager: list[dict[str, Any]], trust_manager: list[dict[str, Any]]) -> None:
    _install(
        kubectl,
        cert_manager,
        {
            "apiVersion": "cert-manager.io/v1",
            "kind": "ClusterIssuer",
            "metadata": {"name": "test-cert-manager-admission"},
            "spec": {"selfSigned": {}},
        },
    )
    _install(
        kubectl,
        trust_manager,
        {
            "apiVersion": "trust.cert-manager.io/v1alpha1",
            "kind": "Bundle",
            "metadata": {"name": "test-trust-manager-admission"},
            "spec": {
                "sources": [{"secret": {"name": "test-runner-root", "key": "ca.crt"}}],
                "target": {"configMap": {"key": "ca-certificates.crt"}},
            },
        },
    )


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


def wait_for_issuance(kubectl: Kubectl, namespace: str, bundle: str) -> None:
    kubectl(
        "-n", namespace, "wait", "--for=condition=Ready", "certificate/agentplane-runner-ca",
        "certificate/agentplane-runner-gateway", f"--timeout={READY_SECONDS}s",
    )  # fmt: skip
    kubectl("wait", "--for=condition=Synced", f"bundle/{bundle}", f"--timeout={READY_SECONDS}s")
