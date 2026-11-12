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


def _template_spec(*, pod: bool = False, vm: bool = True, firecracker: bool = False) -> dict[str, Any]:
    spec: dict[str, Any] = {"stateVolume": {"name": "state", "claim": {}, "layout": {"type": "DirectoryTree"}}}
    if pod:
        spec["pod"] = {}
    if vm:
        spec["vm"] = {}
    if firecracker:
        spec["firecracker"] = {
            "pod": {"metadata": {"labels": {"retained": "yes"}}},
            "bootImage": "example.invalid/boot:v1",
            "runnerImage": "example.invalid/runner:v1",
            "vcpuCount": 2,
            "memoryMiB": 2048,
        }
        spec["stateVolume"]["layout"] = {"type": "RawDiskImage", "filesystem": "ext4"}
        spec["workspaceVolume"] = {
            "name": "workspace",
            "claim": {"volumeMode": "Filesystem"},
            "layout": {"type": "RawDiskImage", "filesystem": "ext4"},
        }
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

        for index, (pod, vm, firecracker) in enumerate(
            [
                (False, False, False),
                (True, False, False),
                (False, True, False),
                (False, False, True),
                (True, True, False),
                (True, False, True),
                (False, True, True),
                (True, True, True),
            ]
        ):
            spec = _template_spec(pod=pod, vm=vm, firecracker=firecracker)
            created = await _create(api, "sandboxtemplates", f"kind-{index}", spec, sum((pod, vm, firecracker)) == 1)
            if firecracker and created is not None:
                assert created["spec"]["firecracker"]["pod"] == spec["firecracker"]["pod"]

        for index, (state, fc_workspace_layout, valid) in enumerate(
            [
                ({"type": "DirectoryTree"}, None, False),
                ({"type": "RawDiskImage", "filesystem": "ext4"}, {"type": "DirectoryTree"}, False),
                ({"type": "RawDiskImage", "filesystem": "ext4"}, {"type": "RawDiskImage", "filesystem": "ext4"}, True),
                (
                    {"type": "RawDiskImage", "filesystem": "ext4"},
                    {"type": "BlockFilesystem", "filesystem": "ext4"},
                    True,
                ),
            ]
        ):
            spec = _template_spec(vm=False, firecracker=True)
            spec["stateVolume"]["layout"] = state
            if fc_workspace_layout is not None:
                fc_mode = "Block" if fc_workspace_layout["type"] == "BlockFilesystem" else "Filesystem"
                spec["workspaceVolume"] = {
                    "name": "workspace",
                    "claim": {"volumeMode": fc_mode},
                    "layout": fc_workspace_layout,
                }
            await _create(api, "sandboxtemplates", f"firecracker-layout-{index}", spec, valid)

        no_workspace = _template_spec(vm=False, firecracker=True)
        del no_workspace["workspaceVolume"]
        await _create(api, "sandboxtemplates", "firecracker-no-workspace", no_workspace, False)

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

        # The controller records Firecracker's raw state layout through the status subresource.
        for index, (layout, valid) in enumerate(
            [
                ({"type": "DirectoryTree"}, True),
                ({"type": "RawDiskImage", "filesystem": "ext4"}, True),
                ({"type": "DirectoryTree", "filesystem": "ext4"}, False),
                ({"type": "RawDiskImage"}, False),
                ({"type": "RawDiskImage", "filesystem": "xfs"}, False),
                ({"type": "BlockFilesystem", "filesystem": "ext4"}, False),
            ]
        ):
            name = f"state-binding-{index}"
            bound = await _create(api, "sandboxes", name, {"template": "firecracker-layout-2"}, True)
            assert bound is not None
            candidate = {
                **bound,
                "status": {"volumes": {"state": {"name": "state", "uid": "state-uid", "layout": layout}}},
            }
            if valid:
                accepted = await api.replace_namespaced_custom_object_status(
                    GROUP, VERSION, NAMESPACE, "sandboxes", name, candidate
                )
                assert accepted["status"]["volumes"]["state"]["layout"] == layout
            else:
                with pytest.raises(ApiException) as rejected:
                    await api.replace_namespaced_custom_object_status(
                        GROUP, VERSION, NAMESPACE, "sandboxes", name, candidate
                    )
                assert rejected.value.status == 422, f"{layout}: {rejected.value.body!r}"

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


async def test_identity_changes_only_while_nothing_runs() -> None:
    async with kubernetes_apiserver() as server:
        core = k8s_client.CoreV1Api(server.api)
        await core.create_namespace(k8s_client.V1Namespace(metadata=k8s_client.V1ObjectMeta(name=NAMESPACE)))
        api = k8s_client.CustomObjectsApi(server.api)
        await _create(api, "sandboxtemplates", "template", _template_spec(pod=True, vm=False), True)
        sandbox = await _create(api, "sandboxes", "bound", {"template": "template"}, True)
        assert sandbox is not None

        async def replace_spec(spec: dict[str, Any]) -> dict[str, Any]:
            nonlocal sandbox
            assert sandbox is not None
            sandbox = cast(
                dict[str, Any],
                await api.replace_namespaced_custom_object(
                    GROUP, VERSION, NAMESPACE, "sandboxes", "bound", {**sandbox, "spec": spec}
                ),
            )
            return sandbox

        async def report(status: dict[str, Any]) -> None:
            nonlocal sandbox
            assert sandbox is not None
            sandbox = cast(
                dict[str, Any],
                await api.replace_namespaced_custom_object_status(
                    GROUP, VERSION, NAMESPACE, "sandboxes", "bound", {**sandbox, "status": status}
                ),
            )

        first: dict[str, Any] = {"template": "template", "identity": {"name": "first"}}
        second: dict[str, Any] = {"template": "template", "identity": {"name": "second"}}
        # Never reconciled: nothing has run.
        await replace_spec(first)

        await report({"operatingMode": "Running"})
        unbound: dict[str, Any] = {"template": "template"}
        for spec in [second, unbound]:
            with pytest.raises(ApiException) as rejected:
                await replace_spec(spec)
            assert rejected.value.status == 422, f"{spec}: {rejected.value.body!r}"
        assert (await replace_spec({**first, "running": True}))["spec"]["identity"] == first["identity"]

        await report({"operatingMode": "Suspended", "suspendedBy": "Requested", "stopStage": "Quiescing"})
        with pytest.raises(ApiException) as rejected:
            await replace_spec(second)
        assert rejected.value.status == 422, repr(rejected.value.body)

        await report({"operatingMode": "Suspended", "suspendedBy": "Requested", "stopStage": "Stopped"})
        assert (await replace_spec(second))["spec"]["identity"] == second["identity"]
        assert "identity" not in (await replace_spec(unbound))["spec"]


async def test_egress_credential_source_and_target_validation() -> None:
    async with kubernetes_apiserver() as server:
        core = k8s_client.CoreV1Api(server.api)
        await core.create_namespace(k8s_client.V1Namespace(metadata=k8s_client.V1ObjectMeta(name=NAMESPACE)))
        api = k8s_client.CustomObjectsApi(server.api)

        async def create(name: str, source: dict[str, Any], targets: list[dict[str, str]], valid: bool) -> None:
            body = {
                "apiVersion": f"{GROUP}/{VERSION}",
                "kind": "EgressCredential",
                "metadata": {"name": name},
                "spec": {"description": "API token", "source": source, "targets": targets},
            }
            if not valid:
                with pytest.raises(ApiException) as rejected:
                    await api.create_namespaced_custom_object(GROUP, VERSION, NAMESPACE, "egresscredentials", body)
                assert rejected.value.status == 422, f"{name}: {rejected.value.status}: {rejected.value.body!r}"
                return
            await api.create_namespaced_custom_object(GROUP, VERSION, NAMESPACE, "egresscredentials", body)

        source_cases: list[tuple[dict[str, Any], bool]] = [
            ({"secretRef": {"name": "api-token", "key": "token"}}, True),
            ({"kubernetesServiceAccount": {"namespace": "accounts", "name": "diagnostics"}}, True),
            ({}, False),
            ({"authenticatedWorkloadToken": {}}, False),
            ({"projectedWorkloadToken": {"audience": "kubernetes"}}, False),
            (
                {
                    "secretRef": {"name": "api-token", "key": "token"},
                    "kubernetesServiceAccount": {"namespace": "accounts", "name": "diagnostics"},
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
            await create(f"source-{index}", source, [{"method": "wholeValue", "header": "Authorization"}], valid)
        for index, (target, valid) in enumerate(target_cases):
            await create(f"target-{index}", secret, [target], valid)

        # The account source substitutes the proxy's own bearer, so it admits that one target only.
        account = {"namespace": "accounts", "name": "shared-diagnostics"}
        bearer = {"method": "schemeToken", "header": "Authorization", "scheme": "Bearer"}
        account_cases: list[tuple[dict[str, Any], list[dict[str, str]], bool]] = [
            ({"kubernetesServiceAccount": account}, [bearer], True),
            (
                {"kubernetesServiceAccount": account},
                [{"method": "schemeToken", "header": "authorization", "scheme": "bearer"}],
                True,
            ),
            ({"kubernetesServiceAccount": account}, [{"method": "wholeValue", "header": "Authorization"}], False),
            ({"kubernetesServiceAccount": account}, [{"method": "jsonField", "field": "token"}], False),
            (
                {"kubernetesServiceAccount": account},
                [{"method": "schemeToken", "header": "X-Token", "scheme": "Bearer"}],
                False,
            ),
            ({"kubernetesServiceAccount": account}, [bearer, bearer], False),
            ({"kubernetesServiceAccount": {**account, "namespace": "Accounts"}}, [bearer], False),
            ({"kubernetesServiceAccount": {**account, "name": "a:b"}}, [bearer], False),
            ({"kubernetesServiceAccount": {"namespace": "accounts"}}, [bearer], False),
            ({"kubernetesServiceAccount": account, **secret}, [bearer], False),
        ]
        for index, (source, targets, valid) in enumerate(account_cases):
            await create(f"account-{index}", source, targets, valid)


if __name__ == "__main__":
    pytest_bazel.main()
