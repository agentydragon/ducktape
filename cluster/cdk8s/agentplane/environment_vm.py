"""The runner's VM SandboxTemplate: the runner on the NixOS guest (agentplane/env/images/vm.nix), in a
KubeVirt VM the Sandbox Controller makes from `spec.vm` (agentplane/environments/SPEC.md § VMs).

The guest gets what a runner Pod's container gets, as the disks vm.nix reads by serial: the public
launch config (platform instructions included), the egress trust bundle, the runner
and `agentplane` command on their own read-only disk, and the Sandbox-owned workspace disk, which
survives VM replacements. The controller attaches its PVC and
adds the state PVC as the virtiofs filesystem named after the state volume, along with the Sandbox's
own launch file as one more disk. The egress relay the guest dials at the masquerade gateway is the launcher policy's
(launcher_relay.py); the VM carries the runner Pods' labels, so the runner fence applies to its
launcher.
"""

from __future__ import annotations

from typing import Any

from cdk8s import ApiObject, ApiObjectMetadata, JsonPatch
from cdk8s_plus_34 import ConfigMap
from constructs import Construct
from kubevirt_virtualmachine_crds.io.kubevirt import (
    VirtualMachine,
    VirtualMachineSpec,
    VirtualMachineSpecTemplateSpecDomainCpu,
    VirtualMachineSpecTemplateSpecDomainDevicesInterfacesPorts,
    VirtualMachineSpecTemplateSpecDomainFirmware,
    VirtualMachineSpecTemplateSpecDomainResources,
    VirtualMachineSpecTemplateSpecDomainResourcesRequests,
    VirtualMachineSpecTemplateSpecVolumes,
    VirtualMachineSpecTemplateSpecVolumesConfigMap,
    VirtualMachineSpecTemplateSpecVolumesContainerDisk,
)

from agentplane.crd_group import GROUP
from cluster.cdk8s.agentplane.environment import Environment
from cluster.cdk8s.kubevirt.virtual_machine import container_disk_template

NAME = "environment-vm"
ROOT_IMAGE = "git.allegedly.works/ducktape-ci/agentplane-environment-vm:unset"  # image-pins/ sets the tag
RUNNER_DISK_IMAGE = "git.allegedly.works/ducktape-ci/agentplane-runner-disk:unset"  # image-pins/ sets the tag
_WORKSPACE = "workspace"
# A listed port keeps masquerade from forwarding every other port, including kubelet's probe of
# the relay's readiness port, while exposing the Runner mTLS listener.
_UNUSED_GUEST_PORT = 22
_ENVIRONMENT_RUNTIME_PORT = 9090


def _memory_quantity(memory_bytes: int) -> str:
    mebibyte = 1024 * 1024
    return f"{memory_bytes // mebibyte}Mi" if memory_bytes % mebibyte == 0 else str(memory_bytes)


class EnvironmentVmTemplate(Construct):
    """Build one VM SandboxTemplate from an explicitly composed guest ConfigMap."""

    def __init__(
        self,
        scope: Construct,
        id: str,
        env: Environment,
        *,
        template_name: str,
        root_image: str,
        config_map: ConfigMap,
        labels: dict[str, str],
        state_volume: dict[str, Any],
        workspace_volume: dict[str, Any],
        firmware: VirtualMachineSpecTemplateSpecDomainFirmware | None = None,
        root_disk_serial: str | None = None,
    ) -> None:
        super().__init__(scope, id)
        namespace = env.namespace
        resources = env.vm_resources
        if resources is None:
            raise ValueError("VM Sandboxes require vm_resources")
        template = container_disk_template(
            labels=labels,
            image=root_image,
            cpu=VirtualMachineSpecTemplateSpecDomainCpu(cores=resources.guest_vcpu_count),
            resources=VirtualMachineSpecTemplateSpecDomainResources(
                requests={
                    "memory": VirtualMachineSpecTemplateSpecDomainResourcesRequests.from_string(
                        _memory_quantity(resources.guest_memory_bytes)
                    )
                }
            ),
            ports=[
                VirtualMachineSpecTemplateSpecDomainDevicesInterfacesPorts(port=_UNUSED_GUEST_PORT),
                VirtualMachineSpecTemplateSpecDomainDevicesInterfacesPorts(port=_ENVIRONMENT_RUNTIME_PORT),
            ],
            disks={
                "agentplane-config": VirtualMachineSpecTemplateSpecVolumes(
                    name="config", config_map=VirtualMachineSpecTemplateSpecVolumesConfigMap(name=config_map.name)
                ),
                # Holds `ca-certificates.crt` and `ca-certificates.p12`, the names vm.nix mounts.
                "agentplane-trust": VirtualMachineSpecTemplateSpecVolumes(
                    name="trust",
                    config_map=VirtualMachineSpecTemplateSpecVolumesConfigMap(name=env.egress.ca_secret_name),
                ),
                # An erofs with the runner and `agentplane` command, which vm.nix mounts executable.
                # The guest finds this disk via virtio-agentplane-runner in its fstab. Keep that
                # device serial stable; it names the payload disk, not the Runtime service.
                "agentplane-runner": VirtualMachineSpecTemplateSpecVolumes(
                    name="runner",
                    container_disk=VirtualMachineSpecTemplateSpecVolumesContainerDisk(
                        image=RUNNER_DISK_IMAGE, image_pull_policy="IfNotPresent"
                    ),
                ),
            },
            controller_attached_disks=[_WORKSPACE],
            firmware=firmware,
            root_disk_serial=root_disk_serial,
            autoattach_graphics_device=False,
        )
        vm = VirtualMachine.manifest(
            metadata=ApiObjectMetadata(name=template_name), spec=VirtualMachineSpec(template=template)
        )["spec"]
        ApiObject(
            self,
            "sandboxtemplate",
            api_version=f"{GROUP}/v1alpha1",
            kind="SandboxTemplate",
            metadata=ApiObjectMetadata(name=template_name, namespace=namespace),
        ).add_json_patch(
            JsonPatch.add("/spec", {"vm": vm, "stateVolume": state_volume, "workspaceVolume": workspace_volume})
        )
