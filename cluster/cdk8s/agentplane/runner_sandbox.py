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
from pathlib import Path
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
from cluster.cdk8s.service_ref import Pods

# Every runner template's Pod name label, and the runners' ServiceAccount and fence.
NAME = "agentplane-runner"
_IMAGE = "git.allegedly.works/ducktape-ci/runner"
_PLACEHOLDER_TAG = "unset"  # always overridden by image-pins/kustomization.yaml
_LABELS = {"app.kubernetes.io/name": NAME}
# Shared by the runner's --state-dir flag, its container volumeMount, and the
# SandboxTemplate's state volume -- all three must name the same volume.
_STATE_VOLUME_NAME = "state"
_STATE_DIR = "/state"
_WORKSPACE_VOLUME_NAME = "workspace"
_WORKSPACE_DIR = f"{_STATE_DIR}/workspaces"
_BUILDBUDDY_SECRET = "buildbuddy-api-key"
_BUILDBUDDY_MOUNT = "/run/buildbuddy"
KUBERNETES_ADMIN_INSTRUCTIONS = (
    Path(__file__).with_name("kubernetes_admin_instructions.md").read_text(encoding="utf-8").strip()
)
# The deployment-wide instructions every Session starts with, as a ConfigMap the runner container mounts.
_PLATFORM_INSTRUCTIONS_CONFIG_MAP = "agentplane-runner-platform-instructions"
_PLATFORM_INSTRUCTIONS_KEY = "platform-instructions.md"
_PLATFORM_INSTRUCTIONS_VOLUME = "platform-instructions"
_PLATFORM_INSTRUCTIONS_DIR = "/etc/agentplane/platform-instructions"
# Where the Sandbox controller mounts the Sandbox's launch ConfigMap (agentplane/sandbox_controller/launch.go).
_LAUNCH_FILE = "/run/agentplane/sandbox/launch.json"


def platform_instructions(env: Environment) -> str:
    """The complete deployment-wide prompt: shared guidance with this environment's service URLs."""
    actions_service = actions.service(env.namespace)
    shared = render_platform_instructions(
        egress_api_url=f"http://{egress.agent_api(env.namespace).fqdn}",
        actions_service_url=f"http://{actions_service.fqdn}:{actions_service.port.number}",
        notifications_service_url=f"http://{notifications.service(env.namespace).fqdn}:8080",
    )
    return f"{shared}\n\n{KUBERNETES_ADMIN_INSTRUCTIONS}"


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
    """The runners' ServiceAccount and fence, the shared runner template, and where the environment has
    VM Sandboxes, the VM template and its launchers' relay."""

    def __init__(self, scope: Construct, id: str, env: Environment) -> None:
        super().__init__(scope, id)
        namespace = env.namespace
        # The runner Pods' identity, with no RBAC of its own.
        ServiceAccount(
            self, "serviceaccount", metadata=ApiObjectMetadata(name=NAME, namespace=namespace), automount_token=False
        )
        # Runner Pods accept no connections; they reach DNS and the egress proxy's listener, which the
        # sidecar relays to. The platform routes add the Agentplane services their runner dials.
        NetworkPolicy(
            self,
            "networkpolicy",
            metadata=ApiObjectMetadata(name=NAME, namespace=namespace),
            endpoint_selector=pods(namespace).selector,
            ingress=isolate_ingress(),
            egress=[cilium.dns_egress(), egress.proxy(namespace).egress()],
        )
        platform_routes.PlatformRouteFences(self, "platform-routes", namespace=namespace, clients=pods(namespace))
        ConfigMap(
            self,
            "platform-instructions",
            metadata=ApiObjectMetadata(name=_PLATFORM_INSTRUCTIONS_CONFIG_MAP, namespace=namespace),
            data={_PLATFORM_INSTRUCTIONS_KEY: platform_instructions(env)},
        )
        template = RunnerTemplate(self, "runner-template", env, name="runner", image=_IMAGE)
        if env.vm_sandboxes:
            launcher_relay.LauncherRelayPolicy(
                self, "launcher-relay", namespace=namespace, runner_pod=template.spec["pod"]
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
                platform_instructions=platform_instructions(env),
            )


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
    ) -> None:
        super().__init__(scope, id)
        self.env = env
        self.buildbuddy_secret = buildbuddy_secret
        self.extra_harness_env = extra_harness_env
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
                    # Neither a workload token nor a LiteLLM key: the placeholder the
                    # agentplane-workload EgressCredential derives from its name. Central
                    # substitutes the sidecar-only projected token; the ingress replaces
                    # that with its server-held LiteLLM virtual key after live
                    # WorkloadPrincipal resolution.
                    k8s.EnvVar(name="ANTHROPIC_AUTH_TOKEN", value="agentplane-credential-agentplane-workload"),
                    k8s.EnvVar(name="OPENAI_API_KEY", value="agentplane-credential-agentplane-workload"),
                ],
            ),
            resources=k8s.ResourceRequirements(
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
                *sandbox_pod.egress_mounts(),
                *(
                    [k8s.VolumeMount(name=_BUILDBUDDY_SECRET, mount_path=_BUILDBUDDY_MOUNT, read_only=True)]
                    if self.buildbuddy_secret
                    else []
                ),
            ],
        )

    def _add_sandbox_template(self, *, name: str, image: str) -> dict[str, Any]:
        pod = sandbox_pod.render_pod_template(
            _LABELS,
            sandbox_pod.pod_spec(
                self.env,
                workload=self._runner_container(image),
                service_account_name=NAME,
                workload_volumes=[
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
                sidecar_env=[platform_routes.sidecar_env(self.env.namespace)],
            ),
        )
        state_volume = {
            "name": _STATE_VOLUME_NAME,
            "claim": {
                # The bulk tier, and the only one a sandbox can have: OVH's `tier=ssd` nodes are
                # control-plane, and a sandbox is not getting that toleration. Node-local, so a sandbox
                # does not outlive its node -- which is what a sandbox is for.
                "storageClassName": "local-path-ovh-hdd",
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
