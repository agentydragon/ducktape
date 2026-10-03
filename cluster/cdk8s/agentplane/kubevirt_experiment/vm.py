"""Small halted VM used to exercise admission independently of the runner image."""

from cdk8s import ApiObjectMetadata
from cdk8s_plus_34 import k8s
from constructs import Construct
from kubevirt_virtualmachine_crds.io import kubevirt as vm

from cluster.cdk8s.agentplane.kubevirt_experiment.policy import MANAGED_LABEL, SERVICE_ACCOUNT_ANNOTATION
from cluster.cdk8s.providers.kubevirt.virtual_machine import VirtualMachine


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
