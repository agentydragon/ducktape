"""KubeVirt VM constructs for the admission and runner runtime experiments."""

from collections.abc import Mapping

from cdk8s import ApiObjectMetadata
from cdk8s_plus_34 import k8s
from constructs import Construct
from kubevirt_virtualmachine_crds.io import kubevirt as vm
from kubevirt_virtualmachine_crds.io.kubevirt import (
    VirtualMachineSpecTemplate,
    VirtualMachineSpecTemplateSpec,
    VirtualMachineSpecTemplateSpecDomain,
    VirtualMachineSpecTemplateSpecDomainCpu,
    VirtualMachineSpecTemplateSpecDomainDevices,
    VirtualMachineSpecTemplateSpecDomainDevicesDisks,
    VirtualMachineSpecTemplateSpecDomainDevicesDisksDisk,
    VirtualMachineSpecTemplateSpecDomainDevicesInterfaces,
    VirtualMachineSpecTemplateSpecDomainDevicesInterfacesPorts,
    VirtualMachineSpecTemplateSpecDomainFirmware,
    VirtualMachineSpecTemplateSpecDomainFirmwareBootloader,
    VirtualMachineSpecTemplateSpecDomainFirmwareBootloaderEfi,
    VirtualMachineSpecTemplateSpecDomainResources,
    VirtualMachineSpecTemplateSpecDomainResourcesRequests,
    VirtualMachineSpecTemplateSpecNetworks,
    VirtualMachineSpecTemplateSpecNetworksPod,
    VirtualMachineSpecTemplateSpecVolumes,
    VirtualMachineSpecTemplateSpecVolumesConfigMap,
    VirtualMachineSpecTemplateSpecVolumesContainerDisk,
    VirtualMachineSpecTemplateSpecVolumesPersistentVolumeClaim,
)

from cluster.cdk8s.agentplane.kubevirt_experiment.policy import MANAGED_LABEL, SERVICE_ACCOUNT_ANNOTATION
from cluster.cdk8s.providers.kubevirt.virtual_machine import VirtualMachine

_RUNNER_TEMPLATE = "prototype"
_VM_TEMPLATE_ANNOTATION = "agentplane.allegedly.works/vm-template"


def prototype_vm(scope: Construct, *, namespace: str, name: str, image: str, public_key: str, uid: str | None) -> None:
    labels = {MANAGED_LABEL: "true", "kubevirt.io/domain": name}
    VirtualMachine(
        scope,
        name,
        metadata=ApiObjectMetadata(
            name=name,
            namespace=namespace,
            labels=labels,
            annotations={SERVICE_ACCOUNT_ANNOTATION: name, "agentplane.allegedly.works/vm-template": "prototype"},
        ),
        run_strategy="Halted",
        template=vm.VirtualMachineSpecTemplate(
            metadata=k8s.ObjectMeta(labels=labels),
            spec=vm.VirtualMachineSpecTemplateSpec(
                node_selector={"topology.kubernetes.io/zone": "hil-ovh"},
                domain=vm.VirtualMachineSpecTemplateSpecDomain(
                    cpu=vm.VirtualMachineSpecTemplateSpecDomainCpu(cores=1),
                    resources=vm.VirtualMachineSpecTemplateSpecDomainResources(
                        requests={
                            "memory": vm.VirtualMachineSpecTemplateSpecDomainResourcesRequests.from_string("768Mi")
                        }
                    ),
                    devices=vm.VirtualMachineSpecTemplateSpecDomainDevices(
                        disks=[
                            vm.VirtualMachineSpecTemplateSpecDomainDevicesDisks(
                                name=n, disk=vm.VirtualMachineSpecTemplateSpecDomainDevicesDisksDisk(bus="virtio")
                            )
                            for n in ["root", "cloud-init"]
                        ],
                        interfaces=[
                            vm.VirtualMachineSpecTemplateSpecDomainDevicesInterfaces(
                                name="default",
                                masquerade={},
                                ports=[
                                    vm.VirtualMachineSpecTemplateSpecDomainDevicesInterfacesPorts(port=p)
                                    for p in [22, 7000]
                                ],
                            )
                        ],
                    ),
                ),
                networks=[
                    vm.VirtualMachineSpecTemplateSpecNetworks(
                        name="default", pod=vm.VirtualMachineSpecTemplateSpecNetworksPod()
                    )
                ],
                volumes=[
                    vm.VirtualMachineSpecTemplateSpecVolumes(
                        name="root", container_disk=vm.VirtualMachineSpecTemplateSpecVolumesContainerDisk(image=image)
                    ),
                    vm.VirtualMachineSpecTemplateSpecVolumes(
                        name="cloud-init",
                        cloud_init_no_cloud=vm.VirtualMachineSpecTemplateSpecVolumesCloudInitNoCloud(
                            user_data="#cloud-config\nusers:\n  - name: prototype\n    groups: wheel\n    sudo: ALL=(ALL) NOPASSWD:ALL\n    ssh_authorized_keys:\n      - "
                            + public_key.strip()
                            + "\n"
                        ),
                    ),
                ],
            ),
        ),
    )
    if uid is not None:
        k8s.KubeServiceAccount(
            scope,
            f"{name}-account",
            automount_service_account_token=False,
            metadata=k8s.ObjectMeta(
                name=name,
                namespace=namespace,
                owner_references=[
                    k8s.OwnerReference(
                        api_version="kubevirt.io/v1", kind="VirtualMachine", name=name, uid=uid, controller=True
                    )
                ],
            ),
        )


def runner_vm(
    scope: Construct,
    *,
    namespace: str,
    name: str,
    image: str,
    image_pull_secret: str,
    cpu_cores: int,
    memory: str,
    state_claim: str,
    workspace_claim: str,
    config_map: str,
    trust_config_map: str,
    node_selector: Mapping[str, str],
) -> VirtualMachine:
    """Build the prototype runner guest from explicit, public fixture inputs."""
    labels = {MANAGED_LABEL: "true"}
    volumes = [
        VirtualMachineSpecTemplateSpecVolumes(
            name="root",
            container_disk=VirtualMachineSpecTemplateSpecVolumesContainerDisk(
                image=image, image_pull_secret=image_pull_secret, image_pull_policy="IfNotPresent"
            ),
        ),
        VirtualMachineSpecTemplateSpecVolumes(
            name="state",
            persistent_volume_claim=VirtualMachineSpecTemplateSpecVolumesPersistentVolumeClaim(claim_name=state_claim),
        ),
        VirtualMachineSpecTemplateSpecVolumes(
            name="workspace",
            persistent_volume_claim=VirtualMachineSpecTemplateSpecVolumesPersistentVolumeClaim(
                claim_name=workspace_claim
            ),
        ),
        VirtualMachineSpecTemplateSpecVolumes(
            name="config", config_map=VirtualMachineSpecTemplateSpecVolumesConfigMap(name=config_map)
        ),
        VirtualMachineSpecTemplateSpecVolumes(
            name="trust", config_map=VirtualMachineSpecTemplateSpecVolumesConfigMap(name=trust_config_map)
        ),
    ]
    disks = [
        VirtualMachineSpecTemplateSpecDomainDevicesDisks(
            name="root", boot_order=1, disk=VirtualMachineSpecTemplateSpecDomainDevicesDisksDisk(bus="virtio")
        ),
        *(
            VirtualMachineSpecTemplateSpecDomainDevicesDisks(
                name=disk, serial=serial, disk=VirtualMachineSpecTemplateSpecDomainDevicesDisksDisk(bus="virtio")
            )
            for disk, serial in (
                ("state", "state"),
                ("workspace", "workspace"),
                ("config", "agentplane-config"),
                ("trust", "agentplane-trust"),
            )
        ),
    ]
    return VirtualMachine(
        scope,
        name,
        metadata=ApiObjectMetadata(
            name=name,
            namespace=namespace,
            labels=labels,
            annotations={SERVICE_ACCOUNT_ANNOTATION: f"vm-{name}-account", _VM_TEMPLATE_ANNOTATION: _RUNNER_TEMPLATE},
        ),
        run_strategy="Halted",
        template=VirtualMachineSpecTemplate(
            metadata=k8s.ObjectMeta(labels=labels),
            spec=VirtualMachineSpecTemplateSpec(
                eviction_strategy="None",
                node_selector=dict(node_selector),
                domain=VirtualMachineSpecTemplateSpecDomain(
                    cpu=VirtualMachineSpecTemplateSpecDomainCpu(cores=cpu_cores),
                    resources=VirtualMachineSpecTemplateSpecDomainResources(
                        requests={"memory": VirtualMachineSpecTemplateSpecDomainResourcesRequests.from_string(memory)}
                    ),
                    firmware=VirtualMachineSpecTemplateSpecDomainFirmware(
                        bootloader=VirtualMachineSpecTemplateSpecDomainFirmwareBootloader(
                            efi=VirtualMachineSpecTemplateSpecDomainFirmwareBootloaderEfi(secure_boot=False)
                        )
                    ),
                    devices=VirtualMachineSpecTemplateSpecDomainDevices(
                        autoattach_graphics_device=False,
                        disks=disks,
                        interfaces=[
                            VirtualMachineSpecTemplateSpecDomainDevicesInterfaces(
                                name="default",
                                masquerade={},
                                ports=[
                                    VirtualMachineSpecTemplateSpecDomainDevicesInterfacesPorts(
                                        name="runner", port=7000, protocol="TCP"
                                    )
                                ],
                            )
                        ],
                    ),
                ),
                networks=[
                    VirtualMachineSpecTemplateSpecNetworks(
                        name="default", pod=VirtualMachineSpecTemplateSpecNetworksPod()
                    )
                ],
                volumes=volumes,
            ),
        ),
    )
