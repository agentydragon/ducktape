"""Ducktape's KubeVirt VirtualMachines, built on `providers/kubevirt`."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from cdk8s import ApiObjectMetadata
from cdk8s_plus_34 import k8s
from constructs import Construct
from kubevirt_virtualmachine_crds.io.kubevirt import (
    VirtualMachineSpecTemplate,
    VirtualMachineSpecTemplateSpec,
    VirtualMachineSpecTemplateSpecAffinity,
    VirtualMachineSpecTemplateSpecDomain,
    VirtualMachineSpecTemplateSpecDomainCpu,
    VirtualMachineSpecTemplateSpecDomainDevices,
    VirtualMachineSpecTemplateSpecDomainDevicesDisks,
    VirtualMachineSpecTemplateSpecDomainDevicesDisksDisk,
    VirtualMachineSpecTemplateSpecDomainDevicesHostDevices,
    VirtualMachineSpecTemplateSpecDomainDevicesInterfaces,
    VirtualMachineSpecTemplateSpecDomainDevicesInterfacesPorts,
    VirtualMachineSpecTemplateSpecDomainFirmware,
    VirtualMachineSpecTemplateSpecDomainFirmwareBootloader,
    VirtualMachineSpecTemplateSpecDomainFirmwareBootloaderEfi,
    VirtualMachineSpecTemplateSpecDomainResources,
    VirtualMachineSpecTemplateSpecNetworks,
    VirtualMachineSpecTemplateSpecNetworksPod,
    VirtualMachineSpecTemplateSpecVolumes,
    VirtualMachineSpecTemplateSpecVolumesContainerDisk,
)

from cluster.cdk8s.providers.kubevirt.virtual_machine import VirtualMachine

_ROOT_DISK = "rootdisk"
_NETWORK = "default"


def domain_labels(name: str) -> dict[str, str]:
    """The labels the pods of VM `name` carry for a Service to select them by."""
    return {"kubevirt.io/domain": name}


def _virtio_disk(
    name: str, *, serial: str | None = None, boot_order: int | None = None
) -> VirtualMachineSpecTemplateSpecDomainDevicesDisks:
    return VirtualMachineSpecTemplateSpecDomainDevicesDisks(
        name=name,
        boot_order=boot_order,
        serial=serial,
        disk=VirtualMachineSpecTemplateSpecDomainDevicesDisksDisk(bus="virtio"),
    )


def container_disk_vm(
    scope: Construct,
    id: str,
    *,
    name: str,
    namespace: str,
    image: str,
    cpu: VirtualMachineSpecTemplateSpecDomainCpu,
    resources: VirtualMachineSpecTemplateSpecDomainResources,
    node_selector: Mapping[str, str],
    ports: Sequence[VirtualMachineSpecTemplateSpecDomainDevicesInterfacesPorts],
    disks: Mapping[str, VirtualMachineSpecTemplateSpecVolumes],
    run_strategy: str = "Always",
    annotations: Mapping[str, str] | None = None,
    affinity: VirtualMachineSpecTemplateSpecAffinity | None = None,
    host_devices: Sequence[VirtualMachineSpecTemplateSpecDomainDevicesHostDevices] | None = None,
    autoattach_graphics_device: bool | None = None,
) -> VirtualMachine:
    """A VM (always running by default) that boots `image` as an ephemeral containerDisk under EFI without
    Secure Boot, and reaches the pod network through masquerade on `ports`. Each of `disks`
    attaches as a virtio disk the guest finds at `/dev/disk/by-id/virtio-<key>`. The VM is labelled
    with its name, and its pods with `domain_labels(name)` too. `cpu`, `resources`, `node_selector`,
    `affinity`, `host_devices` and `autoattach_graphics_device` are the VMI spec fields of those
    names.
    """
    labels = {"app.kubernetes.io/name": name}
    return VirtualMachine(
        scope,
        id,
        metadata=ApiObjectMetadata(name=name, namespace=namespace, labels=labels, annotations=annotations),
        run_strategy=run_strategy,
        template=VirtualMachineSpecTemplate(
            metadata=k8s.ObjectMeta(labels=domain_labels(name) | labels),
            spec=VirtualMachineSpecTemplateSpec(
                node_selector=node_selector,
                affinity=affinity,
                domain=VirtualMachineSpecTemplateSpecDomain(
                    cpu=cpu,
                    resources=resources,
                    firmware=VirtualMachineSpecTemplateSpecDomainFirmware(
                        bootloader=VirtualMachineSpecTemplateSpecDomainFirmwareBootloader(
                            efi=VirtualMachineSpecTemplateSpecDomainFirmwareBootloaderEfi(secure_boot=False)
                        )
                    ),
                    devices=VirtualMachineSpecTemplateSpecDomainDevices(
                        autoattach_graphics_device=autoattach_graphics_device,
                        disks=[
                            _virtio_disk(_ROOT_DISK, boot_order=1),
                            *(_virtio_disk(volume.name, serial=serial) for serial, volume in disks.items()),
                        ],
                        interfaces=[
                            VirtualMachineSpecTemplateSpecDomainDevicesInterfaces(
                                name=_NETWORK, masquerade={}, ports=list(ports)
                            )
                        ],
                        host_devices=list(host_devices) if host_devices is not None else None,
                    ),
                ),
                networks=[
                    VirtualMachineSpecTemplateSpecNetworks(
                        name=_NETWORK, pod=VirtualMachineSpecTemplateSpecNetworksPod()
                    )
                ],
                volumes=[
                    VirtualMachineSpecTemplateSpecVolumes(
                        name=_ROOT_DISK,
                        container_disk=VirtualMachineSpecTemplateSpecVolumesContainerDisk(
                            image=image, image_pull_policy="IfNotPresent"
                        ),
                    ),
                    *disks.values(),
                ],
            ),
        ),
    )
