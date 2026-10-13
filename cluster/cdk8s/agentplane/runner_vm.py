"""The runner's VM SandboxTemplate: the runner on the NixOS guest (agentplane/runner/vm.nix), in a
KubeVirt VM the Sandbox Controller makes from `spec.vm` (agentplane/sandbox_controller/SPEC.md § VMs).

The guest gets what a runner Pod's container gets, as the disks vm.nix reads by serial: the public
launch config and kubeconfig, the egress trust bundle, and a blank workspace disk that lives as long
as the VM. The controller adds the state PVC as the virtiofs filesystem named after the state
volume. The egress relay the guest dials at the masquerade gateway is the launcher policy's
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
    VirtualMachineSpecDataVolumeTemplates,
    VirtualMachineSpecDataVolumeTemplatesSpec,
    VirtualMachineSpecDataVolumeTemplatesSpecPvc,
    VirtualMachineSpecDataVolumeTemplatesSpecPvcResources,
    VirtualMachineSpecDataVolumeTemplatesSpecPvcResourcesRequests,
    VirtualMachineSpecDataVolumeTemplatesSpecSource,
    VirtualMachineSpecTemplateSpecDomainCpu,
    VirtualMachineSpecTemplateSpecDomainDevicesInterfacesPorts,
    VirtualMachineSpecTemplateSpecDomainResources,
    VirtualMachineSpecTemplateSpecDomainResourcesRequests,
    VirtualMachineSpecTemplateSpecVolumes,
    VirtualMachineSpecTemplateSpecVolumesConfigMap,
    VirtualMachineSpecTemplateSpecVolumesDataVolume,
)

from agentplane.crd_group import GROUP
from agentplane.runner.guest_config import CA_BUNDLE, Endpoint, GuestConfig
from cluster.cdk8s.agentplane import launcher_relay, llm_ingress, platform_routes, sandbox_pod
from cluster.cdk8s.agentplane.environment import Environment
from cluster.cdk8s.config_format import yaml_config
from cluster.cdk8s.kubevirt.virtual_machine import container_disk_template

NAME = "runner-vm"
_IMAGE = "git.allegedly.works/ducktape-ci/agentplane-runner-vm:unset"  # image-pins/ sets the tag
_CONFIG_MAP_NAME = "agentplane-runner-vm-config"
_WORKSPACE = "workspace"
# The guest listens on nothing; a listed port keeps masquerade from forwarding every other port,
# kubelet's probe of the relay's readiness port among them, to the guest.
_UNUSED_GUEST_PORT = 22


def _guest_address(port: int) -> str:
    return f"{launcher_relay.GUEST_GATEWAY}:{port}"


class RunnerVmTemplate(Construct):
    """`labels` are the runner Pods' labels; `state_volume` is the runner template's `stateVolume`."""

    def __init__(
        self, scope: Construct, id: str, env: Environment, *, labels: dict[str, str], state_volume: dict[str, Any]
    ) -> None:
        super().__init__(scope, id)
        namespace = env.namespace
        config = GuestConfig(
            version=1,
            control=Endpoint(
                target=_guest_address(platform_routes.control(namespace).listen_port),
                bearer=platform_routes.ROUTE_BEARER,
            ),
            ingress=Endpoint(
                target=_guest_address(platform_routes.ingress(namespace).listen_port),
                bearer=platform_routes.ROUTE_BEARER,
            ),
            llm_base_url=llm_ingress.proxied_url(namespace),
            proxy_url=f"http://{_guest_address(sandbox_pod.SIDECAR_LISTEN_PORT)}",
        )
        ConfigMap(
            self,
            "config",
            metadata=ApiObjectMetadata(name=_CONFIG_MAP_NAME, namespace=namespace),
            data={
                "config.json": config.model_dump_json(),
                "kubeconfig": yaml_config(sandbox_pod.kubeconfig(str(CA_BUNDLE))),
            },
        )
        template = container_disk_template(
            labels=labels,
            image=_IMAGE,
            cpu=VirtualMachineSpecTemplateSpecDomainCpu(cores=1),
            # agentplane-testing's 4Gi container limit caps a guest at about 1.7Gi.
            resources=VirtualMachineSpecTemplateSpecDomainResources(
                requests={"memory": VirtualMachineSpecTemplateSpecDomainResourcesRequests.from_string("1536Mi")}
            ),
            node_selector={"kubevirt.io/schedulable": "true"},
            ports=[VirtualMachineSpecTemplateSpecDomainDevicesInterfacesPorts(port=_UNUSED_GUEST_PORT)],
            disks={
                _WORKSPACE: VirtualMachineSpecTemplateSpecVolumes(
                    name=_WORKSPACE, data_volume=VirtualMachineSpecTemplateSpecVolumesDataVolume(name=_WORKSPACE)
                ),
                "agentplane-config": VirtualMachineSpecTemplateSpecVolumes(
                    name="config", config_map=VirtualMachineSpecTemplateSpecVolumesConfigMap(name=_CONFIG_MAP_NAME)
                ),
                # Holds `ca-certificates.crt` and `ca-certificates.p12`, the names vm.nix copies.
                "agentplane-trust": VirtualMachineSpecTemplateSpecVolumes(
                    name="trust",
                    config_map=VirtualMachineSpecTemplateSpecVolumesConfigMap(name=env.egress.ca_secret_name),
                ),
            },
            autoattach_graphics_device=False,
        )
        # The controller renames it per Sandbox; the VM keeps it while halted and deletes it with itself.
        workspace = VirtualMachineSpecDataVolumeTemplates(
            metadata={"name": _WORKSPACE},
            spec=VirtualMachineSpecDataVolumeTemplatesSpec(
                source=VirtualMachineSpecDataVolumeTemplatesSpecSource(blank={}),
                pvc=VirtualMachineSpecDataVolumeTemplatesSpecPvc(
                    # The state PVC's tier, which the VM's node must also reach.
                    storage_class_name=state_volume["claim"]["storageClassName"],
                    access_modes=["ReadWriteOnce"],
                    resources=VirtualMachineSpecDataVolumeTemplatesSpecPvcResources(
                        requests={
                            "storage": VirtualMachineSpecDataVolumeTemplatesSpecPvcResourcesRequests.from_string("10Gi")
                        }
                    ),
                ),
            ),
        )
        vm = VirtualMachine.manifest(
            metadata=ApiObjectMetadata(name=NAME),
            spec=VirtualMachineSpec(template=template, data_volume_templates=[workspace]),
        )["spec"]
        ApiObject(
            self,
            "sandboxtemplate",
            api_version=f"{GROUP}/v1alpha1",
            kind="SandboxTemplate",
            metadata=ApiObjectMetadata(name=NAME, namespace=namespace),
        ).add_json_patch(JsonPatch.add("/spec", {"vm": vm, "stateVolume": state_volume}))
