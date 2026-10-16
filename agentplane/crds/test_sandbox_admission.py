"""Exercise the generated Sandbox schemas through Kubernetes admission, including CEL."""

from copy import deepcopy
from typing import Any

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
        assert rejected.value.status == 422, f"{name}: {rejected.value.status}: {rejected.value.body}"
        return None
    return await api.create_namespaced_custom_object(GROUP, VERSION, NAMESPACE, plural, body)


async def test_generated_sandbox_crds_admit_same_values() -> None:
    async with kubernetes_apiserver() as server:
        core = k8s_client.CoreV1Api(server.api)
        await core.create_namespace({"metadata": {"name": NAMESPACE}})
        api = k8s_client.CustomObjectsApi(server.api)

        for index, (pod, vm, valid) in enumerate(
            [(True, False, True), (False, True, True), (False, False, False), (True, True, False)]
        ):
            await _create(api, "sandboxtemplates", f"kind-{index}", _template_spec(pod=pod, vm=vm), valid)

        workspace_cases: list[tuple[dict[str, str] | None, str | None, bool]] = [
            ({"type": "DirectoryTree"}, None, True),
            ({"type": "DirectoryTree"}, "Filesystem", True),
            ({"type": "DirectoryTree", "filesystem": "ext4"}, None, False),
            ({"type": "DirectoryTree"}, "Block", False),
            ({"type": "RawDiskImage", "filesystem": "ext4"}, None, True),
            ({"type": "RawDiskImage", "filesystem": "ext4"}, "Filesystem", True),
            ({"type": "RawDiskImage"}, None, False),
            ({"type": "RawDiskImage", "filesystem": "xfs"}, None, False),
            ({"type": "RawDiskImage", "filesystem": "ext4"}, "Block", False),
            ({"type": "BlockFilesystem", "filesystem": "ext4"}, "Block", True),
            ({"type": "BlockFilesystem", "filesystem": "ext4"}, None, False),
            ({"type": "BlockFilesystem", "filesystem": "ext4"}, "Filesystem", False),
            ({"type": "Other"}, None, False),
            ({}, None, False),
            (None, None, False),
        ]
        for index, (layout, mode, valid) in enumerate(workspace_cases):
            spec = _template_spec()
            workspace: dict[str, Any] = {"name": "workspace", "claim": {} if mode is None else {"volumeMode": mode}}
            if layout is not None:
                workspace["layout"] = layout
            spec["workspaceVolume"] = workspace
            await _create(api, "sandboxtemplates", f"workspace-{index}", spec, valid)

        for index, (layout, valid) in enumerate(
            [
                (None, False),
                ({"type": "DirectoryTree"}, True),
                ({"type": "DirectoryTree", "filesystem": "ext4"}, False),
                ({"type": "RawDiskImage", "filesystem": "ext4"}, False),
                ({}, False),
            ]
        ):
            spec = _template_spec()
            state = spec["stateVolume"]
            if layout is None:
                del state["layout"]
            else:
                state["layout"] = layout
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

        sandbox = await _create(api, "sandboxes", "defaulted", {"template": "opaque"}, True)
        assert sandbox is not None
        assert sandbox["spec"]["running"] is False
        assert sandbox["spec"]["wakeOnSend"] is False
        assert sandbox["spec"]["wakeGeneration"] == 0
        for index, spec in enumerate(
            [
                {"template": ""},
                {"template": "opaque", "wakeGeneration": -1},
                {"template": "opaque", "policySets": ["same", "same"]},
                {"template": "opaque", "bootstrap": "x" * 65537},
            ]
        ):
            await _create(api, "sandboxes", f"invalid-{index}", spec, False)
        await _create(api, "sandboxes", "n" * 64, {"template": "opaque"}, False)

        with_status = deepcopy(sandbox)
        with_status["status"] = {"suspendedBy": "Idle"}
        with pytest.raises(ApiException) as rejected:
            await api.replace_namespaced_custom_object_status(
                GROUP, VERSION, NAMESPACE, "sandboxes", "defaulted", with_status
            )
        assert rejected.value.status == 422, rejected.value.body

        with_status["status"]["suspendedAtWakeGeneration"] = 0
        await api.replace_namespaced_custom_object_status(
            GROUP, VERSION, NAMESPACE, "sandboxes", "defaulted", with_status
        )


if __name__ == "__main__":
    pytest_bazel.main()
