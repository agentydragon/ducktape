"""VM intent, retained disks, and UID-checked launcher discovery."""

import json
from typing import cast
from uuid import uuid4

import pytest
import pytest_bazel
from kubernetes_asyncio import client as k8s_client
from pydantic import ValidationError

from agentplane.sandbox_service.destinations import DestinationResolver, DestinationUnavailableError
from agentplane.sandbox_service.inventory import SandboxInventory
from agentplane.sandbox_service.kubernetes_views import PROVISIONING_ANNOTATION
from agentplane.sandbox_service.kubevirt import VmTemplate
from agentplane.sandbox_service.models import InventoryError, SandboxNotFoundError
from agentplane.sandbox_service.protocol_pb2 import ENVIRONMENT_KIND_KUBEVIRT, CreateSandboxRequest, SandboxDestination
from agentplane.sandbox_service.testing.fake_inventory import NAMESPACE, FakeCoreV1Api, FakeCustomObjectsApi
from util.kubernetes import CustomObjectsClient

# gazelle:include_dep @pypi//protobuf


def _template() -> VmTemplate:
    return VmTemplate(
        image=f"registry.test/runner@sha256:{'a' * 64}",
        image_pull_secret="test-pull-creds",
        storage_class="test-local-storage",
        llm_base_url="http://llm.test",
        proxy_url="http://10.0.2.2:3128",
        ca_bundle_config_map="test-egress-ca",
        kubernetes_host="kubernetes.test",
        kubernetes_credential_name="test-kubernetes-workload",
    )


def test_vm_template_requires_supported_state_schema() -> None:
    with pytest.raises(ValidationError, match="state_schema_version"):
        VmTemplate.model_validate({**_template().model_dump(), "state_schema_version": 2})


async def test_vm_starts_only_after_dependencies_and_retains_disks_on_delete() -> None:
    custom, core = FakeCustomObjectsApi(), FakeCoreV1Api()
    catalog = {"test-vm": _template()}
    inventory = SandboxInventory(
        namespace=NAMESPACE,
        custom_objects=cast(CustomObjectsClient, custom),
        core_v1=cast(k8s_client.CoreV1Api, core),
        vm_templates=catalog,
    )
    view = await inventory.create(
        CreateSandboxRequest(slug="test-env", template="test-vm", kind=ENVIRONMENT_KIND_KUBEVIRT),
        annotations={"agentplane.allegedly.works/pending-launch-grants": '{"policies":[],"action_policy_sets":[]}'},
    )
    vm = custom.objects[("virtualmachines", view.name)]
    assert view.kind == ENVIRONMENT_KIND_KUBEVIRT
    assert vm["spec"]["runStrategy"] == "Halted"
    domain = vm["spec"]["template"]["spec"]["domain"]
    assert domain["firmware"]["bootloader"]["efi"]["secureBoot"] is False
    assert domain["devices"]["disks"][0]["bootOrder"] == 1
    assert vm["spec"]["template"]["spec"]["readinessProbe"]["tcpSocket"]["port"] == 7000
    account = core.service_accounts[f"vm-{view.name}-account"]
    assert account.metadata.owner_references[0].uid == view.uid
    assert account.metadata.owner_references[0].controller is True
    with pytest.raises(ValueError, match="grants are not ready"):
        await inventory.resume(view.name, kind="kubevirt", uid=view.uid)

    assert await inventory.ensure_vm_dependencies(view)
    config = core.config_maps[f"vm-{view.name}-config"]
    assert json.loads(config.data["config.json"])["environment_id"] == view.uid
    assert json.loads(config.data["config.json"])["format_blank_disks"] == ["state", "workspace"]
    kubeconfig = json.loads(config.data["kubeconfig"])
    assert kubeconfig["users"][0]["user"]["token"] == "agentplane-credential-test-kubernetes-workload"
    for disk in ("state", "workspace"):
        volume = custom.objects[("datavolumes", f"vm-{view.name}-{disk}")]
        assert "ownerReferences" not in volume["metadata"]
        volume["status"] = {"phase": "WaitForFirstConsumer"}
    assert await inventory.ensure_vm_dependencies(view)
    await inventory.suspend(view.name, kind="kubevirt", uid=view.uid)
    await inventory.finish_provisioning(view)
    assert vm["spec"]["runStrategy"] == "Halted"
    await inventory.resume(view.name, kind="kubevirt", uid=view.uid)
    assert vm["spec"]["runStrategy"] == "Always"
    await inventory.retire_vm_disk_initialization(view.name, view.uid)
    assert json.loads(config.data["config.json"])["format_blank_disks"] == []
    assert await inventory.ensure_vm_dependencies(view)
    await inventory.suspend(view.name, kind="kubevirt", uid=view.uid)
    custom.objects[("virtualmachineinstances", view.name)] = {
        "metadata": {
            "name": view.name,
            "namespace": NAMESPACE,
            "uid": str(uuid4()),
            "creationTimestamp": "2026-09-02T10:00:00Z",
            "ownerReferences": [
                {
                    "apiVersion": "kubevirt.io/v1",
                    "kind": "VirtualMachine",
                    "name": view.name,
                    "uid": view.uid,
                    "controller": True,
                }
            ],
        },
        "status": {"phase": "Running"},
    }
    with pytest.raises(InventoryError, match="still stopping"):
        await inventory.resume(view.name, kind="kubevirt", uid=view.uid)
    custom.objects[("virtualmachineinstances", view.name)]["metadata"]["deletionTimestamp"] = "2026-09-02T10:01:00Z"
    with pytest.raises(InventoryError, match="still stopping"):
        await inventory.resume(view.name, kind="kubevirt", uid=view.uid)
    del custom.objects[("virtualmachineinstances", view.name)]
    catalog["test-vm"] = catalog["test-vm"].model_copy(update={"image": f"registry.test/runner@sha256:{'b' * 64}"})
    await inventory.replace_vm_image(view.name, uid=view.uid, template_name="test-vm")
    root = next(volume for volume in vm["spec"]["template"]["spec"]["volumes"] if volume["name"] == "root")
    assert root["containerDisk"]["image"] == f"registry.test/runner@sha256:{'b' * 64}"
    assert vm["spec"]["runStrategy"] == "Halted"
    with pytest.raises(SandboxNotFoundError):
        await inventory.delete(view.name, kind="kubevirt", uid=str(uuid4()))
    await inventory.delete(view.name, kind="kubevirt", uid=view.uid)
    assert ("virtualmachines", view.name) not in custom.objects
    assert all(("datavolumes", f"vm-{view.name}-{disk}") in custom.objects for disk in ("state", "workspace"))


async def test_vm_destination_rejects_forged_vmi_owner_uid() -> None:
    custom, core = FakeCustomObjectsApi(), FakeCoreV1Api()
    inventory = SandboxInventory(
        namespace=NAMESPACE,
        custom_objects=cast(CustomObjectsClient, custom),
        core_v1=cast(k8s_client.CoreV1Api, core),
        vm_templates={"test-vm": _template()},
    )
    view = await inventory.create(
        CreateSandboxRequest(slug="test-env", template="test-vm", kind=ENVIRONMENT_KIND_KUBEVIRT)
    )
    vm = custom.objects[("virtualmachines", view.name)]
    vm["spec"]["runStrategy"] = "Always"
    custom.objects[("virtualmachineinstances", view.name)] = {
        "metadata": {
            "name": view.name,
            "namespace": NAMESPACE,
            "uid": str(uuid4()),
            "creationTimestamp": "2026-09-02T10:00:00Z",
            "ownerReferences": [
                {
                    "apiVersion": "kubevirt.io/v1",
                    "kind": "VirtualMachine",
                    "name": view.name,
                    "uid": str(uuid4()),
                    "controller": True,
                }
            ],
        },
        "status": {"phase": "Running"},
    }
    destination = SandboxDestination(
        owner=view.service_account, sandbox=view.name, sandbox_uid=view.uid, kind=ENVIRONMENT_KIND_KUBEVIRT
    )
    resolver = DestinationResolver(inventory, cast(k8s_client.CoreV1Api, core), 7000)
    with pytest.raises(DestinationUnavailableError):
        await resolver.resolve(destination)


async def test_vm_finishes_provisioning_and_starts_in_one_patch() -> None:
    custom, core = FakeCustomObjectsApi(), FakeCoreV1Api()
    inventory = SandboxInventory(
        namespace=NAMESPACE,
        custom_objects=cast(CustomObjectsClient, custom),
        core_v1=cast(k8s_client.CoreV1Api, core),
        vm_templates={"test-vm": _template()},
    )
    view = await inventory.create(
        CreateSandboxRequest(slug="test-env", template="test-vm", kind=ENVIRONMENT_KIND_KUBEVIRT),
        annotations={PROVISIONING_ANNOTATION: '{"policies":[],"action_policy_sets":[]}'},
    )
    assert await inventory.ensure_vm_dependencies(view)
    custom.patches.clear()
    await inventory.finish_provisioning(view)
    assert len(custom.patches) == 1
    plural, name, patch = custom.patches[0]
    assert (plural, name) == ("virtualmachines", view.name)
    assert patch["metadata"]["annotations"][PROVISIONING_ANNOTATION] is None
    assert patch["spec"]["runStrategy"] == "Always"
    assert patch["metadata"]["uid"] == view.uid
    assert "resourceVersion" in patch["metadata"]


if __name__ == "__main__":
    pytest_bazel.main()
