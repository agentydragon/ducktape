"""Retained cert-manager/trust-manager installation and admission readiness."""

import os
from collections.abc import Callable
from time import monotonic
from typing import Any

import yaml
from python.runfiles import Runfiles
from tenacity import Retrying, retry_if_exception, retry_if_exception_type, stop_after_delay, wait_fixed

from agentplane.system_tests.driver.tracing import tracer
from agentplane.system_tests.k3s.release_manifests import load_release_manifest

Kubectl = Callable[..., str]
NAMESPACE = "cert-manager"
READY_SECONDS = 180


def _manifest(variable: str) -> list[dict[str, Any]]:
    return load_release_manifest(Runfiles.CreateOrRaise().root() / os.environ[variable], variable)


def releases() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    return _manifest("CERT_MANAGER_INSTALL_RLOCATION"), _manifest("TRUST_MANAGER_INSTALL_RLOCATION")


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


def _admission_probes() -> tuple[dict[str, Any], dict[str, Any]]:
    return (
        {
            "apiVersion": "cert-manager.io/v1",
            "kind": "ClusterIssuer",
            "metadata": {"name": "test-cert-manager-admission"},
            "spec": {"selfSigned": {}},
        },
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


def _install(kubectl: Kubectl, objects: list[dict[str, Any]], probe: dict[str, Any]) -> None:
    kubectl("apply", "--server-side", "-f", "-", input_text=yaml.safe_dump_all(objects))
    crds = [f"crd/{doc['metadata']['name']}" for doc in objects if doc["kind"] == "CustomResourceDefinition"]
    # kubectl's condition waiter errors while a new CRD's conditions are still null.
    deadline = monotonic() + READY_SECONDS
    for attempt in Retrying(
        stop=stop_after_delay(READY_SECONDS),
        wait=wait_fixed(1),
        retry=retry_if_exception(
            lambda error: isinstance(error, RuntimeError) and ".status.conditions accessor error: <nil>" in str(error)
        ),
        reraise=True,
    ):
        with attempt:
            kubectl(
                "wait", "--for=condition=Established", *crds,
                f"--timeout={max(0, deadline - monotonic()):.3f}s",
            )  # fmt: skip
    for doc in objects:
        if doc["kind"] == "Deployment":
            kubectl(
                "-n", NAMESPACE, "rollout", "status", f"deployment/{doc['metadata']['name']}",
                f"--timeout={READY_SECONDS}s",
            )  # fmt: skip
    _wait_for_api(kubectl, probe)


@tracer.start_as_current_span("system_test.pki.controllers")
def install(kubectl: Kubectl, cert_manager: list[dict[str, Any]], trust_manager: list[dict[str, Any]]) -> None:
    cert_manager_probe, trust_manager_probe = _admission_probes()
    _install(kubectl, cert_manager, cert_manager_probe)
    _install(kubectl, trust_manager, trust_manager_probe)


def prepare(
    kubectl: Kubectl, cert_manager: list[dict[str, Any]], trust_manager: list[dict[str, Any]], *, warm: bool
) -> None:
    if not warm:
        install(kubectl, cert_manager, trust_manager)
    else:
        for doc in cert_manager + trust_manager:
            if doc["kind"] == "Deployment":
                kubectl(
                    "-n", NAMESPACE, "rollout", "status", f"deployment/{doc['metadata']['name']}",
                    f"--timeout={READY_SECONDS}s",
                )  # fmt: skip
        for probe in _admission_probes():
            _wait_for_api(kubectl, probe)
