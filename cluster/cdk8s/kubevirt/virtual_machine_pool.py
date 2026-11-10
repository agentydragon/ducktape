"""Ducktape's KubeVirt VirtualMachinePools, built on `providers/kubevirt`.

The pool embeds its own generated copy of the VirtualMachine template types, so this mirrors the
subset of `virtual_machine.container_disk_template` that pools use, over the pool's types.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from kubevirt_virtualmachinepool_crds.io.kubevirt.pool import (
    VirtualMachinePoolSpecVirtualMachineTemplate,
    VirtualMachinePoolSpecVirtualMachineTemplateSpec,
    VirtualMachinePoolSpecVirtualMachineTemplateSpecDataVolumeTemplates,
    VirtualMachinePoolSpecVirtualMachineTemplateSpecTemplate,
    VirtualMachinePoolSpecVirtualMachineTemplateSpecTemplateSpec,
    VirtualMachinePoolSpecVirtualMachineTemplateSpecTemplateSpecAffinity,
    VirtualMachinePoolSpecVirtualMachineTemplateSpecTemplateSpecDomain,
    VirtualMachinePoolSpecVirtualMachineTemplateSpecTemplateSpecDomainCpu,
    VirtualMachinePoolSpecVirtualMachineTemplateSpecTemplateSpecDomainDevices,
    VirtualMachinePoolSpecVirtualMachineTemplateSpecTemplateSpecDomainDevicesDisks,
    VirtualMachinePoolSpecVirtualMachineTemplateSpecTemplateSpecDomainDevicesDisksDisk,
    VirtualMachinePoolSpecVirtualMachineTemplateSpecTemplateSpecDomainDevicesInterfaces,
    VirtualMachinePoolSpecVirtualMachineTemplateSpecTemplateSpecDomainDevicesInterfacesPorts,
    VirtualMachinePoolSpecVirtualMachineTemplateSpecTemplateSpecDomainFirmware,
    VirtualMachinePoolSpecVirtualMachineTemplateSpecTemplateSpecDomainFirmwareBootloader,
    VirtualMachinePoolSpecVirtualMachineTemplateSpecTemplateSpecDomainFirmwareBootloaderEfi,
    VirtualMachinePoolSpecVirtualMachineTemplateSpecTemplateSpecDomainResources,
    VirtualMachinePoolSpecVirtualMachineTemplateSpecTemplateSpecNetworks,
    VirtualMachinePoolSpecVirtualMachineTemplateSpecTemplateSpecNetworksPod,
    VirtualMachinePoolSpecVirtualMachineTemplateSpecTemplateSpecTolerations,
    VirtualMachinePoolSpecVirtualMachineTemplateSpecTemplateSpecVolumes,
    VirtualMachinePoolSpecVirtualMachineTemplateSpecTemplateSpecVolumesContainerDisk,
)

_ROOT_DISK = "rootdisk"
_NETWORK = "default"


def _virtio_disk(
    name: str, *, serial: str | None = None, boot_order: int | None = None
) -> VirtualMachinePoolSpecVirtualMachineTemplateSpecTemplateSpecDomainDevicesDisks:
    return VirtualMachinePoolSpecVirtualMachineTemplateSpecTemplateSpecDomainDevicesDisks(
        name=name,
        boot_order=boot_order,
        serial=serial,
        disk=VirtualMachinePoolSpecVirtualMachineTemplateSpecTemplateSpecDomainDevicesDisksDisk(bus="virtio"),
    )


def container_disk_pool_template(
    *,
    labels: Mapping[str, str],
    image: str,
    cpu: VirtualMachinePoolSpecVirtualMachineTemplateSpecTemplateSpecDomainCpu,
    resources: VirtualMachinePoolSpecVirtualMachineTemplateSpecTemplateSpecDomainResources,
    node_selector: Mapping[str, str] | None = None,
    ports: Sequence[VirtualMachinePoolSpecVirtualMachineTemplateSpecTemplateSpecDomainDevicesInterfacesPorts],
    disks: Mapping[str, VirtualMachinePoolSpecVirtualMachineTemplateSpecTemplateSpecVolumes],
    data_volume_templates: Sequence[VirtualMachinePoolSpecVirtualMachineTemplateSpecDataVolumeTemplates] = (),
    run_strategy: str = "Always",
    affinity: VirtualMachinePoolSpecVirtualMachineTemplateSpecTemplateSpecAffinity | None = None,
    tolerations: Sequence[VirtualMachinePoolSpecVirtualMachineTemplateSpecTemplateSpecTolerations] | None = None,
    autoattach_graphics_device: bool | None = None,
) -> VirtualMachinePoolSpecVirtualMachineTemplate:
    """The pool's VM template: each VM, and its pods, are labelled `labels` (the pool's selector must
    match them) and boot `image` as an ephemeral containerDisk under EFI without Secure Boot, reaching
    the pod network through masquerade on `ports`. Each of `disks` attaches as a virtio disk the guest
    finds at `/dev/disk/by-id/virtio-<key>`; a disk backed by one of `data_volume_templates` gets its own
    volume per VM. The remaining argument names are the corresponding VMI fields.
    """
    return VirtualMachinePoolSpecVirtualMachineTemplate(
        metadata={"labels": dict(labels)},
        spec=VirtualMachinePoolSpecVirtualMachineTemplateSpec(
            run_strategy=run_strategy,
            data_volume_templates=list(data_volume_templates),
            template=VirtualMachinePoolSpecVirtualMachineTemplateSpecTemplate(
                metadata={"labels": dict(labels)},
                spec=VirtualMachinePoolSpecVirtualMachineTemplateSpecTemplateSpec(
                    node_selector=dict(node_selector) if node_selector is not None else None,
                    affinity=affinity,
                    tolerations=list(tolerations) if tolerations is not None else None,
                    domain=VirtualMachinePoolSpecVirtualMachineTemplateSpecTemplateSpecDomain(
                        cpu=cpu,
                        resources=resources,
                        firmware=VirtualMachinePoolSpecVirtualMachineTemplateSpecTemplateSpecDomainFirmware(
                            bootloader=VirtualMachinePoolSpecVirtualMachineTemplateSpecTemplateSpecDomainFirmwareBootloader(
                                efi=VirtualMachinePoolSpecVirtualMachineTemplateSpecTemplateSpecDomainFirmwareBootloaderEfi(
                                    secure_boot=False
                                )
                            )
                        ),
                        devices=VirtualMachinePoolSpecVirtualMachineTemplateSpecTemplateSpecDomainDevices(
                            autoattach_graphics_device=autoattach_graphics_device,
                            disks=[
                                _virtio_disk(_ROOT_DISK, boot_order=1),
                                *(_virtio_disk(volume.name, serial=serial) for serial, volume in disks.items()),
                            ],
                            interfaces=[
                                VirtualMachinePoolSpecVirtualMachineTemplateSpecTemplateSpecDomainDevicesInterfaces(
                                    name=_NETWORK, masquerade={}, ports=list(ports)
                                )
                            ],
                        ),
                    ),
                    networks=[
                        VirtualMachinePoolSpecVirtualMachineTemplateSpecTemplateSpecNetworks(
                            name=_NETWORK, pod=VirtualMachinePoolSpecVirtualMachineTemplateSpecTemplateSpecNetworksPod()
                        )
                    ],
                    volumes=[
                        VirtualMachinePoolSpecVirtualMachineTemplateSpecTemplateSpecVolumes(
                            name=_ROOT_DISK,
                            container_disk=VirtualMachinePoolSpecVirtualMachineTemplateSpecTemplateSpecVolumesContainerDisk(
                                image=image, image_pull_policy="IfNotPresent"
                            ),
                        ),
                        *disks.values(),
                    ],
                ),
            ),
        ),
    )
