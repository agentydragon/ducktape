"""The runner's boxes, behind the egress path every agentplane box shares (sandbox_pod.py): the
runner SandboxTemplate (agentplane/sandbox_controller), which adds the harness runner and a state
volume, and the ServiceAccount and fence every runner template's Pods share. staging.py adds a second
template on the ducktape image.

The templates' container images carry the same `unset` placeholder tag as the app's Deployment:
kustomize's `images:` transformer patches by image name across every resource in the Kustomization,
so image-pins/ covers them too.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from cdk8s import ApiObject, ApiObjectMetadata, JsonPatch
from cdk8s_plus_34 import ConfigMap, ServiceAccount, k8s
from constructs import Construct

from agentplane.crd_group import GROUP, VERSION
from agentplane.runner.platform_instructions import render_platform_instructions
from cluster.cdk8s import cilium
from cluster.cdk8s.agentplane import (
    actions,
    egress,
    launcher_relay,
    llm_ingress,
    notifications,
    platform_routes,
    runner_vm,
    sandbox_pod,
)
from cluster.cdk8s.agentplane.environment import Environment
from cluster.cdk8s.providers.cilium.network_policy import NetworkPolicy, isolate_ingress
from cluster.cdk8s.service_ref import Pods, ServiceRef
from cluster.cdk8s.settings_file import SettingsFile

# Every runner template's Pod name label, and the runners' ServiceAccount and fence.
NAME = "runner"
# Sandbox runner Pods can outlive their template revision. Keep the label they already carry so the
# network fences continue to select existing incarnations until they are naturally replaced.
_POD_LABEL_NAME = "agentplane-runner"
_IMAGE = "git.allegedly.works/ducktape-ci/runner"
_PLACEHOLDER_TAG = "unset"  # always overridden by image-pins/kustomization.yaml
_LABELS = {"app.kubernetes.io/name": _POD_LABEL_NAME}
# Shared by the runner's --state-dir flag, its container volumeMount, and the
# SandboxTemplate's state volume -- all three must name the same volume.
_STATE_VOLUME_NAME = "state"
# The runner disk image's `bin/` (agentplane/runner/runner-disk.nix): the runner and the `agentplane`
# command, mounted read-only the way the VM guest mounts the same image's erofs.
_RUNNER_VOLUME_NAME = "runner"
_RUNNER_DIR = "/run/agentplane/runner"
_STATE_DIR = "/state"
_WORKSPACE_VOLUME_NAME = "workspace"
_WORKSPACE_DIR = f"{_STATE_DIR}/workspaces"
_BUILDBUDDY_SECRET = "buildbuddy-api-key"
_BUILDBUDDY_MOUNT = "/run/buildbuddy"
# The deployment-wide instructions every Session starts with, as a ConfigMap the runner container mounts.
_PLATFORM_INSTRUCTIONS_CONFIG_MAP = "runner-platform-instructions"
_PLATFORM_INSTRUCTIONS_KEY = "platform-instructions.md"
_PLATFORM_INSTRUCTIONS_VOLUME = "platform-instructions"
_PLATFORM_INSTRUCTIONS_DIR = "/etc/agentplane/platform-instructions"
# Where the Sandbox controller mounts the Sandbox's launch ConfigMap (agentplane/sandbox_controller/launch.go).
_LAUNCH_FILE = "/run/agentplane/sandbox/launch.json"
# The bearers a Session presents: the placeholders the agentplane-workload and the notifications
# EgressCredentials derive from their names, which the egress proxy substitutes.
_WORKLOAD_PLACEHOLDER = "agentplane-credential-agentplane-workload"
_NOTIFICATIONS_PLACEHOLDER = "agentplane-credential-agentplane-notifications-workload"


@dataclass(frozen=True)
class ServiceUrls:
    """The base URLs of the services a Session calls, as the platform instructions list them."""

    egress: str
    actions: str
    notifications: str


def service_urls(env: Environment, *, egress_api_service: ServiceRef | None = None) -> ServiceUrls:
    actions_service = actions.service(env.namespace)
    egress_api = egress_api_service or egress.agent_api(env.namespace)
    egress_port = f":{egress_api.port.number}" if egress_api.port.number != 80 else ""
    return ServiceUrls(
        egress=f"http://{egress_api.fqdn}{egress_port}",
        actions=f"http://{actions_service.fqdn}:{actions_service.port.number}",
        notifications=f"http://{notifications.service(env.namespace).fqdn}:8080",
    )


def platform_instructions(env: Environment, *, egress_api_service: ServiceRef | None = None) -> str:
    """The complete deployment-wide prompt: shared guidance with this environment's service URLs."""
    urls = service_urls(env, egress_api_service=egress_api_service)
    return render_platform_instructions(
        egress_api_url=urls.egress, actions_service_url=urls.actions, notifications_service_url=urls.notifications
    )


def agent_cli_environment(urls: ServiceUrls) -> dict[str, str]:
    """What the `agentplane` command reads (agentplane/docs/agent_cli.md): each group's base URL and
    the placeholder bearer the egress proxy swaps for the real credential."""
    return {
        "AGENTPLANE_ACTIONS_URL": urls.actions,
        "AGENTPLANE_ACTIONS_CREDENTIAL": _WORKLOAD_PLACEHOLDER,
        "AGENTPLANE_NOTIFICATIONS_URL": urls.notifications,
        "AGENTPLANE_NOTIFICATIONS_CREDENTIAL": _NOTIFICATIONS_PLACEHOLDER,
        "AGENTPLANE_EGRESS_URL": urls.egress,
        "AGENTPLANE_EGRESS_CREDENTIAL": _WORKLOAD_PLACEHOLDER,
    }


def pods(namespace: str) -> Pods:
    """Every runner Pod in one environment's namespace, whichever template it was stamped from."""
    return Pods(namespace=namespace, labels=tuple(_LABELS.items()))


def workspace_volume_spec(
    state_volume: dict[str, Any], *, layout_type: str, filesystem: str | None = None
) -> dict[str, Any]:
    """The Sandbox-owned workspace claim uses the runner's storage class and requested size."""
    layout = {"type": layout_type}
    if filesystem is not None:
        layout["filesystem"] = filesystem
    return {
        "name": _WORKSPACE_VOLUME_NAME,
        "claim": {**state_volume["claim"], "volumeMode": "Filesystem"},
        "layout": layout,
    }


class RunnerSandbox(Construct):
    """The runners' ServiceAccount and template, plus the VM template and relay when enabled.
    Fleet network and route fences are added separately."""

    def __init__(
        self,
        scope: Construct,
        id: str,
        env: Environment,
        *,
        egress_api_service: ServiceRef | None = None,
        storage_class: str = "local-path-ovh-hdd",
        runner_resources: k8s.ResourceRequirements | None = None,
        sidecar_resources: k8s.ResourceRequirements | None = None,
        workload_env: Sequence[k8s.EnvVar] = (),
        image_pull_secret_name: str | None = sandbox_pod.SECRET_NAME,
        trust_mounts: Sequence[k8s.VolumeMount] | None = None,
        control_service: ServiceRef | None = None,
    ) -> None:
        super().__init__(scope, id)
        self.env = env
        namespace = env.namespace
        # The runner Pods' identity, with no RBAC of its own.
        ServiceAccount(
            self, "serviceaccount", metadata=ApiObjectMetadata(name=NAME, namespace=namespace), automount_token=False
        )
        instructions = platform_instructions(env, egress_api_service=egress_api_service)
        ConfigMap(
            self,
            "platform-instructions",
            metadata=ApiObjectMetadata(name=_PLATFORM_INSTRUCTIONS_CONFIG_MAP, namespace=namespace),
            data={_PLATFORM_INSTRUCTIONS_KEY: instructions},
        )
        template = RunnerTemplate(
            self,
            "runner-template",
            env,
            name="runner",
            image=_IMAGE,
            storage_class=storage_class,
            runner_resources=runner_resources,
            sidecar_resources=sidecar_resources,
            workload_env=workload_env,
            image_pull_secret_name=image_pull_secret_name,
            trust_mounts=trust_mounts,
            control_service=control_service,
            egress_api_service=egress_api_service,
        )
        if env.vm_sandboxes:
            launcher_relay.LauncherRelayPolicy(
                self,
                "launcher-relay",
                namespace=namespace,
                runner_pod=template.spec["pod"],
                settings_config_map=template.add_launcher_relay_settings(name="launcher-relay-egress-sidecar"),
            )
            runner_vm.RunnerVmTemplate(
                self,
                "runner-vm-template",
                env,
                labels=_LABELS,
                state_volume=template.spec["stateVolume"],
                workspace_volume=workspace_volume_spec(
                    template.spec["stateVolume"], layout_type="RawDiskImage", filesystem="ext4"
                ),
                platform_instructions=platform_instructions(env, egress_api_service=egress_api_service),
                harness_env=template.agent_cli_env,
            )

    def add_platform_resources(self) -> None:
        """Add the fleet runner network and platform-route fences."""
        namespace = self.env.namespace
        NetworkPolicy(
            self,
            "networkpolicy",
            metadata=ApiObjectMetadata(name=NAME.removeprefix("agentplane-"), namespace=namespace),
            endpoint_selector=pods(namespace).selector,
            ingress=isolate_ingress(),
            egress=[cilium.dns_egress(), egress.proxy(namespace).egress()],
        )
        platform_routes.PlatformRouteFences(self, "platform-routes", namespace=namespace, clients=pods(namespace))


class RunnerTemplate(Construct):
    """Shared runner Pod and storage wiring; staging ducktape may opt into a direct BuildBuddy key."""

    def __init__(
        self,
        scope: Construct,
        id: str,
        env: Environment,
        *,
        name: str,
        image: str,
        buildbuddy_secret: bool = False,
        extra_harness_env: Sequence[str] = (),
        storage_class: str = "local-path-ovh-hdd",
        runner_resources: k8s.ResourceRequirements | None = None,
        sidecar_resources: k8s.ResourceRequirements | None = None,
        workload_env: Sequence[k8s.EnvVar] = (),
        image_pull_secret_name: str | None = sandbox_pod.SECRET_NAME,
        trust_mounts: Sequence[k8s.VolumeMount] | None = None,
        control_service: ServiceRef | None = None,
        egress_api_service: ServiceRef | None = None,
    ) -> None:
        super().__init__(scope, id)
        self.env = env
        self.buildbuddy_secret = buildbuddy_secret
        self.extra_harness_env = extra_harness_env
        self.storage_class = storage_class
        self.runner_resources = runner_resources
        self.sidecar_resources = sidecar_resources
        self.workload_env = workload_env
        self.image_pull_secret_name = image_pull_secret_name
        self.trust_mounts = trust_mounts if trust_mounts is not None else sandbox_pod.egress_mounts()
        self.control_service = control_service
        self.agent_cli_env = agent_cli_environment(service_urls(env, egress_api_service=egress_api_service))
        # The SandboxTemplate's spec: its `pod`, and the `stateVolume` the VM template shares.
        self.spec = self._add_sandbox_template(name=name, image=image)

    def _runner_container(self, image: str) -> k8s.Container:
        # The environment a harness child starts from: a bare NAME takes the runner's
        # value, NAME=value sets one. The routing vars are named rather than set, so the
        # container env below is where they are written once and everything in the Pod
        # agrees -- a harness child by this passthrough, anything else by inheritance.
        # Deployment-wide workload defaults share the same allowlist and are added by
        # sandbox_pod.workload_environment when the container is built. Names the image owns are
        # listed separately, on --harness-inherit-env, so this invocation forwards them without
        # repeating a value the deployment has no business choosing.
        harness_env = list(
            dict.fromkeys(
                [
                    "HOME",
                    "PATH",
                    *self.env.sandbox_workload_env,
                    *(var.name for var in sandbox_pod.egress_env()),
                    *self.agent_cli_env,
                    *self.extra_harness_env,
                ]
            )
        )
        args = [
            "--state-dir",
            _STATE_DIR,
            # Where the runner image (agentplane/runner/image.nix) links nixpkgs' harnesses.
            "--claude-binary",
            "/bin/claude",
            "--anthropic-base-url",
            "$(LITELLM_URL)",
            "--codex-binary",
            "/bin/codex",
            "--openai-base-url",
            "$(LITELLM_URL)/v1",
            # The Runner Gateway and the ingress, over the sidecar's platform routes.
            *platform_routes.runner_args(self.env.namespace),
            "--launch-file",
            _LAUNCH_FILE,
            "--platform-instructions-file",
            f"{_PLATFORM_INSTRUCTIONS_DIR}/{_PLATFORM_INSTRUCTIONS_KEY}",
        ]
        for entry in harness_env:
            args.extend(["--harness-env", entry])
        for name in self.env.harness_inherited_env:
            args.extend(["--harness-inherit-env", name])
        return k8s.Container(
            name="runner",
            image=f"{image}:{_PLACEHOLDER_TAG}",
            args=args,
            # The runner works in absolute paths. This is for a command exec'd in: the sandbox
            # Actions' `runner` boxes start there unless the caller names a directory.
            working_dir=_STATE_DIR,
            security_context=sandbox_pod.workload_security_context(),
            env=sandbox_pod.workload_environment(
                self.env,
                [
                    k8s.EnvVar(name="LITELLM_URL", value=llm_ingress.proxied_url(self.env.namespace)),
                    *(
                        [k8s.EnvVar(name="BBR_BUILDBUDDY_API_KEY_FILE", value=f"{_BUILDBUDDY_MOUNT}/api-key")]
                        if self.buildbuddy_secret
                        else []
                    ),
                    # On the container and not just on the harness children the runner spawns.
                    *sandbox_pod.egress_env(),
                    *(k8s.EnvVar(name=name, value=value) for name, value in self.agent_cli_env.items()),
                    # Neither a workload token nor a LiteLLM key: the placeholder the
                    # agentplane-workload EgressCredential derives from its name. Central
                    # substitutes the sidecar-only projected token; the ingress replaces
                    # that with its server-held LiteLLM virtual key after live
                    # WorkloadPrincipal resolution.
                    k8s.EnvVar(name="ANTHROPIC_AUTH_TOKEN", value=_WORKLOAD_PLACEHOLDER),
                    k8s.EnvVar(name="OPENAI_API_KEY", value=_WORKLOAD_PLACEHOLDER),
                ],
                self.workload_env,
            ),
            resources=self.runner_resources
            or k8s.ResourceRequirements(
                requests={"cpu": k8s.Quantity.from_string("500m"), "memory": k8s.Quantity.from_string("1Gi")},
                limits={"cpu": k8s.Quantity.from_string("2"), "memory": k8s.Quantity.from_string("4Gi")},
            ),
            volume_mounts=[
                k8s.VolumeMount(name=_STATE_VOLUME_NAME, mount_path=_STATE_DIR),
                # Session presets use this same path in Pod and VM Sandboxes.
                k8s.VolumeMount(name=_WORKSPACE_VOLUME_NAME, mount_path=_WORKSPACE_DIR),
                k8s.VolumeMount(
                    name=_PLATFORM_INSTRUCTIONS_VOLUME, mount_path=_PLATFORM_INSTRUCTIONS_DIR, read_only=True
                ),
                k8s.VolumeMount(name=_RUNNER_VOLUME_NAME, mount_path=_RUNNER_DIR, read_only=True),
                *self.trust_mounts,
                *(
                    [k8s.VolumeMount(name=_BUILDBUDDY_SECRET, mount_path=_BUILDBUDDY_MOUNT, read_only=True)]
                    if self.buildbuddy_secret
                    else []
                ),
            ],
        )

    def add_launcher_relay_settings(self, *, name: str) -> str:
        """The sidecar settings of a VM launcher's relay, as ConfigMap `name`: this template's own, but
        listening on every interface, because the guest is outside the Pod's loopback."""
        return self._add_sidecar_settings(name=name, listen_host="0.0.0.0").config_map.name

    def _add_sidecar_settings(self, *, name: str, listen_host: str | None = None) -> SettingsFile:
        return sandbox_pod.add_sidecar_settings(
            self,
            self.env,
            name=name,
            platform_routes=platform_routes.sidecar_routes(self.env.namespace, control_service=self.control_service),
            listen_host=listen_host,
        )

    def _add_sandbox_template(self, *, name: str, image: str) -> dict[str, Any]:
        sidecar_settings = self._add_sidecar_settings(name=f"{name}-egress-sidecar")
        pod = sandbox_pod.render_pod_template(
            _LABELS,
            sandbox_pod.pod_spec(
                self.env,
                workload=self._runner_container(image),
                service_account_name=NAME,
                sidecar_settings=sidecar_settings,
                workload_volumes=[
                    k8s.Volume(
                        name=_RUNNER_VOLUME_NAME,
                        image=k8s.ImageVolumeSource(reference=runner_vm.RUNNER_DISK_IMAGE, pull_policy="IfNotPresent"),
                    ),
                    k8s.Volume(
                        name=_PLATFORM_INSTRUCTIONS_VOLUME,
                        config_map=k8s.ConfigMapVolumeSource(name=_PLATFORM_INSTRUCTIONS_CONFIG_MAP),
                    ),
                    *(
                        [
                            k8s.Volume(
                                name=_BUILDBUDDY_SECRET,
                                secret=k8s.SecretVolumeSource(
                                    secret_name=_BUILDBUDDY_SECRET,
                                    default_mode=288,  # 0440; readable via fsGroup 1000
                                ),
                            )
                        ]
                        if self.buildbuddy_secret
                        else []
                    ),
                ],
                sidecar_env=self.workload_env,
                sidecar_resources=self.sidecar_resources,
                image_pull_secret_name=self.image_pull_secret_name,
            ),
        )
        state_volume = {
            "name": _STATE_VOLUME_NAME,
            "layout": {"type": "DirectoryTree"},
            "claim": {
                # The bulk tier, and the only one a sandbox can have: OVH's `tier=ssd` nodes are
                # control-plane, and a sandbox is not getting that toleration. Node-local, so a sandbox
                # does not outlive its node -- which is what a sandbox is for.
                "storageClassName": self.storage_class,
                "accessModes": ["ReadWriteOnce"],
                "resources": {"requests": {"storage": "10Gi"}},
            },
        }
        spec = {
            "pod": pod,
            "stateVolume": state_volume,
            "workspaceVolume": workspace_volume_spec(state_volume, layout_type="DirectoryTree"),
        }
        ApiObject(
            self,
            "sandboxtemplate",
            api_version=f"{GROUP}/{VERSION}",
            kind="SandboxTemplate",
            metadata=ApiObjectMetadata(name=name, namespace=self.env.namespace),
        ).add_json_patch(JsonPatch.add("/spec", spec))
        return spec
