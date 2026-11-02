"""Exercise the generated Sandbox schemas through Kubernetes admission, including CEL."""

from copy import deepcopy
from typing import Any, cast

import pytest
import pytest_bazel
from kubernetes_asyncio import client as k8s_client
from kubernetes_asyncio.client.exceptions import ApiException

from agentplane.testing.kubernetes_apiserver import kubernetes_apiserver

GROUP = "agentplane.allegedly.works"
VERSION = "v1alpha1"
NAMESPACE = "sandbox-admission"


def _template_spec(*, pod: bool = False, vm: bool = True) -> dict[str, Any]:
    spec: dict[str, Any] = {"stateVolume": {"name": "state", "claim": {}, "layout": {"type": "DirectoryTree"}}}
    if pod:
        spec["pod"] = {}
    if vm:
        spec["vm"] = {}
    return spec


async def _create(
    api: k8s_client.CustomObjectsApi, plural: str, name: str, spec: dict[str, Any], valid: bool
) -> dict[str, Any] | None:
    body = {
        "apiVersion": f"{GROUP}/{VERSION}",
        "kind": "SandboxTemplate" if plural == "sandboxtemplates" else "Sandbox",
        "metadata": {"name": name},
        "spec": spec,
    }
    if not valid:
        with pytest.raises(ApiException) as rejected:
            await api.create_namespaced_custom_object(GROUP, VERSION, NAMESPACE, plural, body)
        assert rejected.value.status == 422, f"{name}: {rejected.value.status}: {rejected.value.body!r}"
        return None
    return cast(dict[str, Any], await api.create_namespaced_custom_object(GROUP, VERSION, NAMESPACE, plural, body))


async def test_sandbox_cel_relationships_and_opaque_fields() -> None:
    async with kubernetes_apiserver() as server:
        core = k8s_client.CoreV1Api(server.api)
        await core.create_namespace(k8s_client.V1Namespace(metadata=k8s_client.V1ObjectMeta(name=NAMESPACE)))
        api = k8s_client.CustomObjectsApi(server.api)

        for index, (pod, vm, valid) in enumerate(
            [(True, False, True), (False, True, True), (False, False, False), (True, True, False)]
        ):
            await _create(api, "sandboxtemplates", f"kind-{index}", _template_spec(pod=pod, vm=vm), valid)

        workspace_cases: list[tuple[dict[str, str], str | None, bool]] = [
            ({"type": "DirectoryTree"}, None, True),
            ({"type": "DirectoryTree"}, "Filesystem", True),
            ({"type": "DirectoryTree", "filesystem": "ext4"}, None, False),
            ({"type": "DirectoryTree"}, "Block", False),
            ({"type": "RawDiskImage", "filesystem": "ext4"}, None, True),
            ({"type": "RawDiskImage", "filesystem": "ext4"}, "Filesystem", True),
            ({"type": "RawDiskImage"}, None, False),
            ({"type": "RawDiskImage", "filesystem": "ext4"}, "Block", False),
            ({"type": "BlockFilesystem", "filesystem": "ext4"}, "Block", True),
            ({"type": "BlockFilesystem", "filesystem": "ext4"}, None, False),
            ({"type": "BlockFilesystem", "filesystem": "ext4"}, "Filesystem", False),
        ]
        for index, (layout, mode, valid) in enumerate(workspace_cases):
            spec = _template_spec()
            workspace: dict[str, Any] = {"name": "workspace", "claim": {} if mode is None else {"volumeMode": mode}}
            workspace["layout"] = layout
            spec["workspaceVolume"] = workspace
            await _create(api, "sandboxtemplates", f"workspace-{index}", spec, valid)

        for index, (layout, valid) in enumerate(
            [
                ({"type": "DirectoryTree"}, True),
                ({"type": "DirectoryTree", "filesystem": "ext4"}, False),
                ({"type": "RawDiskImage", "filesystem": "ext4"}, False),
            ]
        ):
            spec = _template_spec()
            spec["stateVolume"]["layout"] = layout
            await _create(api, "sandboxtemplates", f"state-{index}", spec, valid)

        pod_workspace = _template_spec(pod=True, vm=False)
        pod_workspace["workspaceVolume"] = {
            "name": "workspace",
            "claim": {},
            "layout": {"type": "RawDiskImage", "filesystem": "ext4"},
        }
        await _create(api, "sandboxtemplates", "pod-disk-workspace", pod_workspace, False)

        opaque_spec = _template_spec(pod=True, vm=False)
        opaque_spec["pod"] = {"metadata": {"labels": {"retained": "yes"}}}
        opaque_spec["stateVolume"]["claim"] = {"resources": {"requests": {"storage": "1Gi"}}}
        opaque = await _create(api, "sandboxtemplates", "opaque", opaque_spec, True)
        assert opaque is not None
        assert opaque["spec"]["pod"] == opaque_spec["pod"]
        assert opaque["spec"]["stateVolume"]["claim"] == opaque_spec["stateVolume"]["claim"]

        opaque_spec["workspaceVolume"] = {
            "name": "workspace",
            "claim": {"resources": {"requests": {"storage": "2Gi"}}},
            "layout": {"type": "DirectoryTree"},
        }
        opaque_workspace = await _create(api, "sandboxtemplates", "opaque-workspace", opaque_spec, True)
        assert opaque_workspace is not None
        assert opaque_workspace["spec"]["workspaceVolume"]["claim"] == opaque_spec["workspaceVolume"]["claim"]

        sandbox = await _create(api, "sandboxes", "suspension", {"template": "opaque"}, True)
        assert sandbox is not None
        with_status = deepcopy(sandbox)
        with_status["status"] = {"suspendedBy": "Idle"}
        with pytest.raises(ApiException) as rejected:
            await api.replace_namespaced_custom_object_status(
                GROUP, VERSION, NAMESPACE, "sandboxes", "suspension", with_status
            )
        assert rejected.value.status == 422, repr(rejected.value.body)

        with_status["status"]["suspendedAtWakeGeneration"] = 0
        current = cast(
            dict[str, Any],
            await api.replace_namespaced_custom_object_status(
                GROUP, VERSION, NAMESPACE, "sandboxes", "suspension", with_status
            ),
        )

        for status, valid in [
            ({"suspendedBy": "Requested"}, True),
            ({"idleSuspendBlockers": [{"reason": "ExecutorRelay"}]}, True),
            ({"suspendedBy": "Requested", "suspendedAtWakeGeneration": 0}, False),
            ({"suspendedAtWakeGeneration": 0}, False),
            ({"suspendedBy": "Unrecognised"}, False),
        ]:
            candidate = {**current, "status": status}
            if valid:
                current = cast(
                    dict[str, Any],
                    await api.replace_namespaced_custom_object_status(
                        GROUP, VERSION, NAMESPACE, "sandboxes", "suspension", candidate
                    ),
                )
                continue
            with pytest.raises(ApiException) as rejected:
                await api.replace_namespaced_custom_object_status(
                    GROUP, VERSION, NAMESPACE, "sandboxes", "suspension", candidate
                )
            assert rejected.value.status == 422, f"{status}: {rejected.value.body!r}"

        for index, (idle_suspend_after, valid) in enumerate(
            [("30m", True), ("1h30m0s", True), ("1m", True), ("59s", False), ("0s", False), ("soon", False)]
        ):
            await _create(
                api, "sandboxes", f"idle-{index}", {"template": "opaque", "idleSuspendAfter": idle_suspend_after}, valid
            )


async def test_egress_credential_source_and_target_validation() -> None:
    async with kubernetes_apiserver() as server:
        core = k8s_client.CoreV1Api(server.api)
        await core.create_namespace(k8s_client.V1Namespace(metadata=k8s_client.V1ObjectMeta(name=NAMESPACE)))
        api = k8s_client.CustomObjectsApi(server.api)

        async def create(name: str, source: dict[str, Any], target: dict[str, str], valid: bool) -> None:
            body = {
                "apiVersion": f"{GROUP}/{VERSION}",
                "kind": "EgressCredential",
                "metadata": {"name": name},
                "spec": {"description": "API token", "source": source, "targets": [target]},
            }
            if not valid:
                with pytest.raises(ApiException) as rejected:
                    await api.create_namespaced_custom_object(GROUP, VERSION, NAMESPACE, "egresscredentials", body)
                assert rejected.value.status == 422, f"{name}: {rejected.value.status}: {rejected.value.body!r}"
                return
            await api.create_namespaced_custom_object(GROUP, VERSION, NAMESPACE, "egresscredentials", body)

        source_cases: list[tuple[dict[str, Any], bool]] = [
            ({"secretRef": {"name": "api-token", "key": "token"}}, True),
            ({"authenticatedWorkloadToken": {}}, True),
            ({"projectedWorkloadToken": {"audience": "kubernetes"}}, True),
            ({}, False),
            ({"secretRef": {"name": "api-token", "key": "token"}, "authenticatedWorkloadToken": {}}, False),
            (
                {
                    "secretRef": {"name": "api-token", "key": "token"},
                    "projectedWorkloadToken": {"audience": "kubernetes"},
                },
                False,
            ),
            ({"authenticatedWorkloadToken": {}, "projectedWorkloadToken": {"audience": "kubernetes"}}, False),
            (
                {
                    "secretRef": {"name": "api-token", "key": "token"},
                    "authenticatedWorkloadToken": {},
                    "projectedWorkloadToken": {"audience": "kubernetes"},
                },
                False,
            ),
        ]
        target_cases = [
            ({"method": "wholeValue", "header": "Authorization"}, True),
            ({"method": "schemeToken", "header": "Authorization", "scheme": "Bearer"}, True),
            ({"method": "basicUsername", "header": "Authorization"}, True),
            ({"method": "basicPassword", "header": "Authorization"}, True),
            ({"method": "basicWhole", "header": "Authorization"}, True),
            ({"method": "jsonField", "field": "password"}, True),
            ({"method": "jsonField"}, False),
            ({"method": "jsonField", "field": "password", "header": "Authorization"}, False),
            ({"method": "jsonField", "field": "password", "scheme": "Bearer"}, False),
            ({"method": "jsonField", "field": ""}, False),
            ({"method": "wholeValue", "header": "Authorization", "scheme": "Bearer"}, False),
            ({"method": "wholeValue", "field": "password"}, False),
            ({"method": "schemeToken", "header": "Authorization"}, False),
        ]
        secret = {"secretRef": {"name": "api-token", "key": "token"}}
        for index, (source, valid) in enumerate(source_cases):
            await create(f"source-{index}", source, {"method": "wholeValue", "header": "Authorization"}, valid)
        for index, (target, valid) in enumerate(target_cases):
            await create(f"target-{index}", secret, target, valid)


if __name__ == "__main__":
    pytest_bazel.main()
