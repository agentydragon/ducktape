"""The runner's VM SandboxTemplate: the runner on the NixOS guest (agentplane/runner/vm.nix), in a
KubeVirt VM the Sandbox Controller makes from `spec.vm` (agentplane/sandbox_controller/SPEC.md § VMs).

The guest gets what a runner Pod's container gets, as the disks vm.nix reads by serial: the public
launch config (platform instructions included) and kubeconfig, the egress trust bundle, the runner
and `agentplane` command on their own read-only disk, and the Sandbox-owned workspace disk, which
survives VM replacements. The controller attaches its PVC and
adds the state PVC as the virtiofs filesystem named after the state volume, along with the Sandbox's
own launch file as one more disk. The egress relay the guest dials at the masquerade gateway is the launcher policy's
(launcher_relay.py); the VM carries the runner Pods' labels, so the runner fence applies to its
launcher.
"""

from __future__ import annotations

from collections.abc import Mapping
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
    VirtualMachineSpecTemplateSpecVolumesContainerDisk,
)

from agentplane.crd_group import GROUP
from agentplane.runner import configuration
from agentplane.runner.configuration import (
    CA_BUNDLE,
    JAVA_TRUST_STORE,
    KUBECONFIG,
    CgroupExecutor,
    Configuration,
    Endpoint,
    Harness,
    VMEnvironment,
)
from cluster.cdk8s.agentplane import launcher_relay, llm_ingress, platform_routes, sandbox_pod, telemetry
from cluster.cdk8s.agentplane.environment import Environment
from cluster.cdk8s.config_format import yaml_config
from cluster.cdk8s.kubevirt.virtual_machine import container_disk_template

NAME = "runner-vm"
_IMAGE = "git.allegedly.works/ducktape-ci/agentplane-runner-vm:unset"  # image-pins/ sets the tag
RUNNER_DISK_IMAGE = "git.allegedly.works/ducktape-ci/agentplane-runner-disk:unset"  # image-pins/ sets the tag
_CONFIG_MAP_NAME = "runner-vm-config"
_RUNNER_ENV_FILE = "runner.env"
_WORKSPACE = "workspace"
# The guest listens on nothing; a listed port keeps masquerade from forwarding every other port,
# kubelet's probe of the relay's readiness port among them, to the guest.
_UNUSED_GUEST_PORT = 22


def _guest_address(port: int) -> str:
    return f"{launcher_relay.GUEST_GATEWAY}:{port}"


def _memory_quantity(memory_bytes: int) -> str:
    mebibyte = 1024 * 1024
    return f"{memory_bytes // mebibyte}Mi" if memory_bytes % mebibyte == 0 else str(memory_bytes)


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
        harness_env: Mapping[str, str],
        session_reconcile_concurrency: int | None = None,
    ) -> None:
        super().__init__(scope, id)
        namespace = env.namespace
        resources = env.vm_resources
        if resources is None:
            raise ValueError("VM Sandboxes require vm_resources")
        proxy = f"http://{_guest_address(sandbox_pod.SIDECAR_LISTEN_PORT)}"
        no_proxy = f"127.0.0.1,localhost,{launcher_relay.GUEST_GATEWAY}"
        network = {
            **dict.fromkeys(sandbox_pod.PROXY_VAR_NAMES, proxy),
            **dict.fromkeys(sandbox_pod.NO_PROXY_VAR_NAMES, no_proxy),
            **dict.fromkeys((*sandbox_pod.CA_BUNDLE_VAR_NAMES, "NIX_SSL_CERT_FILE", "PIP_CERT"), str(CA_BUNDLE)),
        }
        llm_url = llm_ingress.proxied_url(namespace)
        config = Configuration(
            version=2,
            session_reconcile_concurrency=session_reconcile_concurrency,
            state_dir="/state",
            workspace_root="/state/workspaces",
            initialization_cwd="/state/workspaces",
            control=Endpoint(
                target=_guest_address(platform_routes.control(namespace).listen_port),
                bearer=platform_routes.ROUTE_BEARER,
            ),
            ingress=Endpoint(
                target=_guest_address(platform_routes.ingress(namespace).listen_port),
                bearer=platform_routes.ROUTE_BEARER,
            ),
            platform_instructions=platform_instructions,
            runner_environment=configuration.EnvironmentVariables(values=network),
            harness_environment=configuration.EnvironmentVariables(
                values={
                    **network,
                    **env.sandbox_workload_env,
                    **harness_env,
                    "HOME": "/state/workspaces/.home",
                    "USER": "agent",
                    "LOGNAME": "agent",
                    "PATH": "/run/current-system/sw/bin:/run/agentplane/runner/bin",
                    "TZDIR": "/usr/share/zoneinfo",
                    "KUBECONFIG": KUBECONFIG,
                    "JAVA_TOOL_OPTIONS": f"-Djavax.net.ssl.trustStore={JAVA_TRUST_STORE} -Djavax.net.ssl.trustStoreType=PKCS12",
                }
            ),
            claude=Harness(
                binary="/run/current-system/sw/bin/claude", base_url=llm_url, token_env="ANTHROPIC_AUTH_TOKEN"
            ),
            codex=Harness(
                binary="/run/current-system/sw/bin/codex", base_url=f"{llm_url}/v1", token_env="OPENAI_API_KEY"
            ),
            environment=VMEnvironment(
                resources=resources,
                console_autologin=env.vm_console_autologin,
                root="/sys/fs/cgroup/system.slice/agentplane-runner.service/workloads",
                agent_uid=107,
                agent_gid=107,
                block_devices=("/dev/disk/by-id/virtio-workspace",),
                executor=CgroupExecutor(
                    url="ws://127.0.0.1:4501", root="/sys/fs/cgroup/system.slice/agentplane-runner.service/executors"
                )
                if resources.executor is not None
                else None,
            ),
        )
        config_map_data = {
            "runner.json": config.model_dump_json(exclude_none=True),
            "kubeconfig": yaml_config(sandbox_pod.kubeconfig(str(CA_BUNDLE))),
            _RUNNER_ENV_FILE: "".join(
                f'{name}="{value}"\n'
                for name, value in sorted(
                    telemetry.service_environment(
                        env.namespace,
                        "agentplane-runner",
                        endpoint=platform_routes.telemetry_endpoint(host=launcher_relay.GUEST_GATEWAY),
                    ).items()
                )
            ),
        }
        ConfigMap(
            self, "config", metadata=ApiObjectMetadata(name=_CONFIG_MAP_NAME, namespace=namespace), data=config_map_data
        )
        template = container_disk_template(
            labels=labels,
            image=_IMAGE,
            cpu=VirtualMachineSpecTemplateSpecDomainCpu(cores=resources.guest_vcpu_count),
            resources=VirtualMachineSpecTemplateSpecDomainResources(
                requests={
                    "memory": VirtualMachineSpecTemplateSpecDomainResourcesRequests.from_string(
                        _memory_quantity(resources.guest_memory_bytes)
                    )
                }
            ),
            ports=[VirtualMachineSpecTemplateSpecDomainDevicesInterfacesPorts(port=_UNUSED_GUEST_PORT)],
            disks={
                "agentplane-config": VirtualMachineSpecTemplateSpecVolumes(
                    name="config", config_map=VirtualMachineSpecTemplateSpecVolumesConfigMap(name=_CONFIG_MAP_NAME)
                ),
                # Holds `ca-certificates.crt` and `ca-certificates.p12`, the names vm.nix mounts.
                "agentplane-trust": VirtualMachineSpecTemplateSpecVolumes(
                    name="trust",
                    config_map=VirtualMachineSpecTemplateSpecVolumesConfigMap(name=env.egress.ca_secret_name),
                ),
                # An erofs with the runner and `agentplane` command, which vm.nix mounts executable.
                "agentplane-runner": VirtualMachineSpecTemplateSpecVolumes(
                    name="runner",
                    container_disk=VirtualMachineSpecTemplateSpecVolumesContainerDisk(
                        image=RUNNER_DISK_IMAGE, image_pull_policy="IfNotPresent"
                    ),
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
