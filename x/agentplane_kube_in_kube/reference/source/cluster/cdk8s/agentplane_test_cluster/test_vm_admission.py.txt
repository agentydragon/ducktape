"""Exercise tenant VM admission against Kubernetes' CEL engine and real CRD schemas."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable, Callable
from copy import deepcopy
from pathlib import Path
from typing import Any, cast
from uuid import UUID

import cdk8s
import pytest_bazel
import yaml
from cdk8s import App, Chart
from kubernetes_asyncio import client as k8s_client
from kubernetes_asyncio.client.exceptions import ApiException
from tenacity import AsyncRetrying, stop_after_delay, wait_fixed

from agentplane.system_tests.shared_cluster.namespaces import TENANT_LABEL, TENANT_VALUE, Namespaces
from agentplane.testing.kubernetes_apiserver import kubernetes_apiserver
from cluster.cdk8s.agentplane_test_cluster import vm_admission
from util.bazel.runfiles import get_required_path

_GROUP = "kubevirt.io"
_VERSION = "v1"
_DENIAL_MESSAGE = "Tenant VM specifications may not use host devices, guest service accounts, or KubeVirt hooks."
_HOOK_ANNOTATION = "hooks.kubevirt.io/hookSidecars"
_TENANT = Namespaces(UUID("0123456789abcdef0123456789abcdef"))


def _objects(app: App) -> list[dict[str, Any]]:
    chart = Chart(app, "vm-admission")
    vm_admission.add_vm_admission(chart)
    return cast(list[dict[str, Any]], cdk8s.Testing.synth(chart))


def test_vm_admission_is_fail_closed_and_tenant_scoped(scratch_app: App) -> None:
    objects = _objects(scratch_app)
    policies = {obj["metadata"]["name"]: obj for obj in objects if obj["kind"] == "ValidatingAdmissionPolicy"}
    bindings = {obj["metadata"]["name"]: obj for obj in objects if obj["kind"] == "ValidatingAdmissionPolicyBinding"}

    assert set(policies) == {
        "agentplane-test-cluster-tenant-vm-fence-virtualmachines",
        "agentplane-test-cluster-tenant-vm-fence-virtualmachineinstances",
    }
    assert set(bindings) == set(policies)
    for name, policy in policies.items():
        assert policy["spec"]["failurePolicy"] == "Fail"
        assert policy["spec"]["matchConstraints"]["resourceRules"] == [
            {
                "apiGroups": [_GROUP],
                "apiVersions": [_VERSION],
                "operations": ["CREATE", "UPDATE"],
                "resources": [name.rsplit("-", 1)[-1]],
                "scope": "Namespaced",
            }
        ]
        binding = bindings[name]
        assert binding["spec"]["policyName"] == name
        assert binding["spec"]["validationActions"] == ["Deny"]


def _vm(name: str, namespace: str) -> dict[str, Any]:
    return {
        "apiVersion": f"{_GROUP}/{_VERSION}",
        "kind": "VirtualMachine",
        "metadata": {"name": name, "namespace": namespace},
        "spec": {
            "runStrategy": "Halted",
            "template": {
                "metadata": {"labels": {"test": "vm-admission"}},
                "spec": {
                    "domain": {"devices": {}},
                    "volumes": [{"name": "root", "containerDisk": {"image": "example.invalid/test:unused"}}],
                },
            },
        },
    }


def _vmi(name: str, namespace: str) -> dict[str, Any]:
    return {
        "apiVersion": f"{_GROUP}/{_VERSION}",
        "kind": "VirtualMachineInstance",
        "metadata": {"name": name, "namespace": namespace},
        "spec": {
            "domain": {"devices": {}},
            "volumes": [{"name": "root", "containerDisk": {"image": "example.invalid/test:unused"}}],
        },
    }


def _merge(target: dict[str, Any], patch: dict[str, Any]) -> None:
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            _merge(target[key], value)
        else:
            target[key] = deepcopy(value)


def _guest_patch(resource: str, value: dict[str, Any]) -> dict[str, Any]:
    if resource == "virtualmachines":
        return {"spec": {"template": {"spec": value}}}
    return {"spec": value}


def _attacks(resource: str) -> list[tuple[str, dict[str, Any]]]:
    result = [
        (
            "host-disk",
            _guest_patch(resource, {"volumes": [{"name": "host", "hostDisk": {"path": "/etc", "type": "Disk"}}]}),
        ),
        (
            "service-account-volume",
            _guest_patch(
                resource, {"volumes": [{"name": "account", "serviceAccount": {"serviceAccountName": "tenant"}}]}
            ),
        ),
        (
            "host-device",
            _guest_patch(
                resource,
                {"domain": {"devices": {"hostDevices": [{"name": "host", "deviceName": "example.invalid/host"}]}}},
            ),
        ),
        (
            "gpu",
            _guest_patch(
                resource, {"domain": {"devices": {"gpus": [{"name": "gpu", "deviceName": "example.invalid/gpu"}]}}}
            ),
        ),
        ("client-passthrough", _guest_patch(resource, {"domain": {"devices": {"clientPassthrough": {}}}})),
        ("top-level-hook", {"metadata": {"annotations": {_HOOK_ANNOTATION: "[]"}}}),
    ]
    if resource == "virtualmachines":
        result.append(
            ("template-hook", {"spec": {"template": {"metadata": {"annotations": {_HOOK_ANNOTATION: "[]"}}}}})
        )
    return result


async def _denied(operation: Awaitable[object]) -> bool:
    error: ApiException | None = None
    try:
        await operation
    except ApiException as caught:
        error = caught
    if error is None:
        return False
    assert error.status in (403, 422), error.body
    assert _DENIAL_MESSAGE in str(error.body), error.body
    return True


async def _wait_for_denial(operation: Callable[[], Awaitable[object]]) -> None:
    async for attempt in AsyncRetrying(stop=stop_after_delay(30), wait=wait_fixed(0.1), reraise=True):
        with attempt:
            assert await _denied(operation())


async def _install_crds(api: k8s_client.ApiextensionsV1Api) -> None:
    for env_var, crd_name in (
        ("VIRTUALMACHINE_CRD_RLOCATION", "virtualmachines.kubevirt.io"),
        ("VIRTUALMACHINEINSTANCE_CRD_RLOCATION", "virtualmachineinstances.kubevirt.io"),
    ):
        path = get_required_path(os.environ[env_var])
        definition = cast(Any, yaml.safe_load(await asyncio.to_thread(Path(path).read_text)))
        await api.create_custom_resource_definition(definition)
        async for attempt in AsyncRetrying(stop=stop_after_delay(30), wait=wait_fixed(0.1), reraise=True):
            with attempt:
                crd = await api.read_custom_resource_definition(crd_name)
                assert any(
                    condition.type == "Established" and condition.status == "True"
                    for condition in (crd.status.conditions or [])
                ), f"{crd_name} is not established"


async def test_vm_and_vmi_policies_reject_host_access_and_hooks_on_create_and_patch(
    scratch_app_factory: Callable[[], App],
) -> None:
    objects = _objects(scratch_app_factory())
    policies = [obj for obj in objects if obj["kind"] == "ValidatingAdmissionPolicy"]
    bindings = [obj for obj in objects if obj["kind"] == "ValidatingAdmissionPolicyBinding"]

    async with kubernetes_apiserver() as server:
        core = k8s_client.CoreV1Api(server.api)
        for namespace in _TENANT.all:
            await core.create_namespace(
                k8s_client.V1Namespace(
                    metadata=k8s_client.V1ObjectMeta(name=namespace, labels={TENANT_LABEL: TENANT_VALUE})
                )
            )
        await core.create_namespace(k8s_client.V1Namespace(metadata=k8s_client.V1ObjectMeta(name="unprotected")))
        await _install_crds(k8s_client.ApiextensionsV1Api(server.api))

        admission = k8s_client.AdmissionregistrationV1Api(server.api)
        for policy in policies:
            await admission.create_validating_admission_policy(cast(Any, policy))
        for binding in bindings:
            await admission.create_validating_admission_policy_binding(cast(Any, binding))

        custom = k8s_client.CustomObjectsApi(server.api)
        namespace = _TENANT.application

        outside_attack = _vm("vm-admission-outside-label", "unprotected")
        _merge(outside_attack, _attacks("virtualmachines")[0][1])
        await custom.create_namespaced_custom_object(_GROUP, _VERSION, "unprotected", "virtualmachines", outside_attack)

        first_resource = "virtualmachines"
        first_name = "vm-admission-create-host-disk"
        first_attack = _vm(first_name, namespace)
        _merge(first_attack, _attacks(first_resource)[0][1])
        await _wait_for_denial(
            lambda: custom.create_namespaced_custom_object(
                _GROUP, _VERSION, namespace, first_resource, first_attack, dry_run="All"
            )
        )

        for resource, make_object in (("virtualmachines", _vm), ("virtualmachineinstances", _vmi)):
            for attack_name, patch in _attacks(resource):
                name = f"vm-admission-{resource.removesuffix('s')}-{attack_name}"
                benign = make_object(name, namespace)
                await custom.create_namespaced_custom_object(_GROUP, _VERSION, namespace, resource, benign)

                attacked = deepcopy(benign)
                _merge(attacked, patch)
                assert await _denied(
                    custom.create_namespaced_custom_object(
                        _GROUP, _VERSION, namespace, resource, attacked, dry_run="All"
                    )
                )

                assert await _denied(
                    cast(Any, custom).patch_namespaced_custom_object(
                        _GROUP,
                        _VERSION,
                        namespace,
                        resource,
                        name,
                        patch,
                        dry_run="All",
                        _content_type="application/merge-patch+json",
                    )
                )


if __name__ == "__main__":
    pytest_bazel.main()
