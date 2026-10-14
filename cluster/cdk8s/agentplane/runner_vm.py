"""The runner's VM SandboxTemplate: the runner on the NixOS guest (agentplane/runner/vm.nix), in a
KubeVirt VM the Sandbox Controller makes from `spec.vm` (agentplane/sandbox_controller/SPEC.md § VMs).

The guest gets what a runner Pod's container gets, as the disks vm.nix reads by serial: the public
launch config (platform instructions included) and kubeconfig, the egress trust bundle, and the
Sandbox-owned workspace disk, which survives VM replacements. The controller attaches its PVC and
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
    VirtualMachineSpecTemplateSpecDomainResources,
    VirtualMachineSpecTemplateSpecDomainResourcesRequests,
    VirtualMachineSpecTemplateSpecVolumes,
    VirtualMachineSpecTemplateSpecVolumesConfigMap,
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
    """Build the VM SandboxTemplate and the guest configuration it carries."""

    def __init__(
        self,
        scope: Construct,
        id: str,
        env: Environment,
        *,
        labels: dict[str, str],
        state_volume: dict[str, Any],
        workspace_volume: dict[str, Any],
        platform_instructions: str,
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
            platform_instructions=platform_instructions,
            console_autologin=env.vm_console_autologin,
        )
        ConfigMap(
            self,
            "config",
            metadata=ApiObjectMetadata(name=_CONFIG_MAP_NAME, namespace=namespace),
            data={
                # Defaults are omitted: an image that predates a field rejects it, so only an environment
                # that sets it needs the image that reads it.
                "config.json": config.model_dump_json(exclude_defaults=True),
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
                "agentplane-config": VirtualMachineSpecTemplateSpecVolumes(
                    name="config", config_map=VirtualMachineSpecTemplateSpecVolumesConfigMap(name=_CONFIG_MAP_NAME)
                ),
                # Holds `ca-certificates.crt` and `ca-certificates.p12`, the names vm.nix copies.
                "agentplane-trust": VirtualMachineSpecTemplateSpecVolumes(
                    name="trust",
                    config_map=VirtualMachineSpecTemplateSpecVolumesConfigMap(name=env.egress.ca_secret_name),
                ),
            },
            controller_attached_disks=[_WORKSPACE],
            autoattach_graphics_device=False,
        )
        vm = VirtualMachine.manifest(metadata=ApiObjectMetadata(name=NAME), spec=VirtualMachineSpec(template=template))[
            "spec"
        ]
        ApiObject(
            self,
            "sandboxtemplate",
            api_version=f"{GROUP}/v1alpha1",
            kind="SandboxTemplate",
            metadata=ApiObjectMetadata(name=NAME, namespace=namespace),
        ).add_json_patch(
            JsonPatch.add("/spec", {"vm": vm, "stateVolume": state_volume, "workspaceVolume": workspace_volume})
        )
