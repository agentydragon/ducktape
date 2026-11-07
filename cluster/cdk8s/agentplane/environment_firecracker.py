"""Firecracker SandboxTemplate: guest disks and a host-side launcher and egress relay."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from cdk8s import ApiObject, ApiObjectMetadata, JsonPatch
from cdk8s_plus_34 import ConfigMap, k8s
from constructs import Construct

from agentplane.crd_group import GROUP, VERSION
from agentplane.egress import sidecar_config
from agentplane.egress.platform_routes import PLATFORM_ROUTES_PORT
from agentplane.env.config import CodexReadiness
from agentplane.firecracker.config import Config
from cluster.cdk8s.agentplane import platform_routes, sandbox_pod, workload_routes
from cluster.cdk8s.agentplane.environment import Environment
from cluster.cdk8s.agentplane.environment_guest import guest_config_data
from cluster.cdk8s.config_format import json_config
from cluster.cdk8s.providers.cilium.network_policy import Entity, IngressRule, NetworkPolicy
from cluster.cdk8s.settings_file import SettingsFile
from util.settings_contract import validate_settings_fragment

NAME = "environment-firecracker"
BOOT_IMAGE = "git.allegedly.works/ducktape-ci/agentplane-environment-firecracker:unset"
LAUNCHER_IMAGE = "git.allegedly.works/ducktape-ci/agentplane-firecracker-launcher:unset"
GATEWAY = "192.0.2.1"


class EnvironmentFirecrackerTemplate(Construct):
    def __init__(
        self,
        scope: Construct,
        id: str,
        env: Environment,
        *,
        labels: Mapping[str, str],
        state_volume: dict[str, Any],
        workspace_volume: dict[str, Any],
        runner_image: str,
        platform_instructions: str,
        harness_env: Mapping[str, str],
        boot_image: str = BOOT_IMAGE,
        launcher_image: str = LAUNCHER_IMAGE,
        image_pull_secret_name: str | None = sandbox_pod.SECRET_NAME,
        session_reconcile_concurrency: int | None = None,
        codex_readiness: CodexReadiness | None = None,
    ) -> None:
        super().__init__(scope, id)
        resources = env.vm_resources
        if resources is None:
            raise ValueError("Firecracker requires vm_resources")
        namespace = env.namespace
        self.configuration = ConfigMap(
            self,
            "guest-config",
            metadata=ApiObjectMetadata(name=f"{NAME}-config", namespace=namespace),
            data=guest_config_data(
                env,
                gateway=GATEWAY,
                workload_api_socket="unix:///run/agentplane/environment-runtime-tls/workload.sock",
                block_devices=("/dev/vdd",),
                platform_instructions=platform_instructions,
                harness_env=harness_env,
                session_reconcile_concurrency=session_reconcile_concurrency,
                codex_readiness=codex_readiness,
            ),
        )
        relay = sandbox_pod.add_sidecar_settings(
            self,
            env,
            name=f"{NAME}-relay",
            listen_address=f"[::]:{sandbox_pod.SIDECAR_LISTEN_PORT}",
            platform_routes=platform_routes.sidecar_routes(namespace, listen_host="0.0.0.0"),
            workload_routes=workload_routes.sidecar_routes(namespace),
            proxy_transport=sidecar_config.TLSProxyTransport(
                ca_file=Path(sidecar_config.RUNNER_TLS_MOUNT_PATH) / "ca.crt",
                auth=sidecar_config.SpiffeProxyAuth(
                    cert_file=Path(sidecar_config.RUNNER_TLS_MOUNT_PATH) / "tls.crt",
                    key_file=Path(sidecar_config.RUNNER_TLS_MOUNT_PATH) / "tls.key",
                ),
            ),
        )
        launcher_settings = SettingsFile(
            self,
            "launcher-settings",
            metadata=ApiObjectMetadata(name=f"{NAME}-launcher", namespace=namespace),
            path="/run/agentplane/firecracker/config.json",
            content=json_config(
                validate_settings_fragment(
                    Config,
                    {"relay_port": sandbox_pod.SIDECAR_LISTEN_PORT, "platform_route_ports": [PLATFORM_ROUTES_PORT]},
                )
            ),
        )
        wiring = launcher_settings.pod_wiring(volume_name="firecracker-settings", env="FIRECRACKER_CONFIG_FILE")
        memory = resources.guest_memory_bytes + 512 * 1024 * 1024
        launcher = k8s.Container(
            name="environment-runtime",
            image=launcher_image,
            args=["--config", "/run/agentplane/firecracker/config.json"],
            env=[wiring.path_env],
            security_context=k8s.SecurityContext(
                privileged=True, allow_privilege_escalation=True, run_as_user=0, run_as_group=0, run_as_non_root=False
            ),
            resources=k8s.ResourceRequirements(
                requests={
                    "cpu": k8s.Quantity.from_number(resources.guest_vcpu_count),
                    "memory": k8s.Quantity.from_number(memory),
                    "devices.kubevirt.io/kvm": k8s.Quantity.from_number(1),
                },
                limits={
                    "cpu": k8s.Quantity.from_number(resources.guest_vcpu_count),
                    "memory": k8s.Quantity.from_number(memory),
                    "devices.kubevirt.io/kvm": k8s.Quantity.from_number(1),
                },
            ),
            readiness_probe=k8s.Probe(
                http_get=k8s.HttpGetAction(path="/readyz", port=k8s.IntOrString.from_number(9092)),
                period_seconds=2,
                timeout_seconds=1,
                failure_threshold=1,
            ),
            volume_mounts=[
                wiring.mount,
                k8s.VolumeMount(name="guest-config", mount_path="/run/agentplane/config", read_only=True),
                k8s.VolumeMount(name="guest-trust", mount_path="/run/agentplane/trust", read_only=True),
                k8s.VolumeMount(name="tun", mount_path="/dev/net/tun"),
            ],
        )
        pod = sandbox_pod.pod_spec(
            env,
            workload=launcher,
            sidecar_settings=relay,
            image_pull_secret_name=image_pull_secret_name,
            keep_sidecar_until_workload_exit=True,
            restart_policy="Never",
            security_context=k8s.PodSecurityContext(run_as_user=0, run_as_group=0, fs_group=1000),
            workload_volumes=[
                wiring.volume,
                k8s.Volume(name="guest-config", config_map=k8s.ConfigMapVolumeSource(name=self.configuration.name)),
                k8s.Volume(name="guest-trust", config_map=k8s.ConfigMapVolumeSource(name=env.egress.ca_secret_name)),
                k8s.Volume(name="tun", host_path=k8s.HostPathVolumeSource(path="/dev/net/tun", type="CharDevice")),
            ],
        )
        NetworkPolicy(
            self,
            "launcher-status",
            metadata=ApiObjectMetadata(name=f"{NAME}-status", namespace=namespace),
            endpoint_selector={**labels, "agentplane.allegedly.works/environment-backend": "firecracker"},
            ingress=[IngressRule.from_entities(Entity.KUBE_APISERVER, Entity.HOST, ports=[9092])],
        )
        self.template = ApiObject(
            self,
            "Resource",
            api_version=f"{GROUP}/{VERSION}",
            kind="SandboxTemplate",
            metadata=ApiObjectMetadata(name=NAME, namespace=namespace),
        )
        self.template.add_json_patch(
            JsonPatch.add(
                "/spec",
                {
                    "firecracker": {
                        "pod": sandbox_pod.render_pod_template(labels, pod),
                        "bootImage": boot_image,
                        "runnerImage": runner_image,
                        "vcpuCount": resources.guest_vcpu_count,
                        "memoryMiB": resources.guest_memory_bytes // (1024 * 1024),
                    },
                    "stateVolume": state_volume,
                    "workspaceVolume": workspace_volume,
                },
            )
        )
