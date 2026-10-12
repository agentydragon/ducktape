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
from typing import Any

from agent_sandbox_sandboxtemplate_crds.io.x_k8s.agents.extensions import (
    SandboxTemplateSpecPodTemplateSpecContainers,
    SandboxTemplateSpecPodTemplateSpecContainersEnv,
    SandboxTemplateSpecPodTemplateSpecContainersResources,
    SandboxTemplateSpecPodTemplateSpecContainersResourcesLimits,
    SandboxTemplateSpecPodTemplateSpecContainersResourcesRequests,
    SandboxTemplateSpecPodTemplateSpecContainersVolumeMounts,
    SandboxTemplateSpecPodTemplateSpecVolumes,
    SandboxTemplateSpecPodTemplateSpecVolumesSecret,
)
from cdk8s import ApiObject, ApiObjectMetadata, JsonPatch
from cdk8s_plus_34 import ServiceAccount
from constructs import Construct

from agentplane.crd_group import GROUP, VERSION
from cluster.cdk8s import cilium
from cluster.cdk8s.agentplane import egress, launcher_relay, llm_ingress, platform_routes, runner_vm, sandbox_pod
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
_BUILDBUDDY_SECRET = "buildbuddy-api-key"
_BUILDBUDDY_MOUNT = "/run/buildbuddy"


def pods(namespace: str) -> Pods:
    """Every runner Pod in one environment's namespace, whichever template it was stamped from."""
    return Pods(namespace=namespace, labels=tuple(_LABELS.items()))


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
        template = RunnerTemplate(self, "runner-template", env, name="runner", image=_IMAGE)
        if env.vm_sandboxes:
            launcher_relay.LauncherRelayPolicy(
                self, "launcher-relay", namespace=namespace, runner_pod=template.spec["pod"]
            )
            runner_vm.RunnerVmTemplate(
                self, "runner-vm-template", env, labels=_LABELS, state_volume=template.spec["stateVolume"]
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

    def _runner_container(self, image: str) -> SandboxTemplateSpecPodTemplateSpecContainers:
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
        ]
        for entry in harness_env:
            args.extend(["--harness-env", entry])
        for name in self.env.harness_inherited_env:
            args.extend(["--harness-inherit-env", name])
        return SandboxTemplateSpecPodTemplateSpecContainers(
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
                    SandboxTemplateSpecPodTemplateSpecContainersEnv(
                        name="LITELLM_URL", value=llm_ingress.proxied_url(self.env.namespace)
                    ),
                    *(
                        [
                            SandboxTemplateSpecPodTemplateSpecContainersEnv(
                                name="BBR_BUILDBUDDY_API_KEY_FILE", value=f"{_BUILDBUDDY_MOUNT}/api-key"
                            )
                        ]
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
                    SandboxTemplateSpecPodTemplateSpecContainersEnv(
                        name="ANTHROPIC_AUTH_TOKEN", value="agentplane-credential-agentplane-workload"
                    ),
                    SandboxTemplateSpecPodTemplateSpecContainersEnv(
                        name="OPENAI_API_KEY", value="agentplane-credential-agentplane-workload"
                    ),
                ],
            ),
            resources=SandboxTemplateSpecPodTemplateSpecContainersResources(
                requests={
                    "cpu": SandboxTemplateSpecPodTemplateSpecContainersResourcesRequests.from_string("500m"),
                    "memory": SandboxTemplateSpecPodTemplateSpecContainersResourcesRequests.from_string("1Gi"),
                },
                limits={
                    "cpu": SandboxTemplateSpecPodTemplateSpecContainersResourcesLimits.from_string("2"),
                    "memory": SandboxTemplateSpecPodTemplateSpecContainersResourcesLimits.from_string("4Gi"),
                },
            ),
            volume_mounts=[
                SandboxTemplateSpecPodTemplateSpecContainersVolumeMounts(
                    name=_STATE_VOLUME_NAME, mount_path=_STATE_DIR
                ),
                *sandbox_pod.egress_mounts(),
                *(
                    [
                        SandboxTemplateSpecPodTemplateSpecContainersVolumeMounts(
                            name=_BUILDBUDDY_SECRET, mount_path=_BUILDBUDDY_MOUNT, read_only=True
                        )
                    ]
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
                workload_volumes=(
                    [
                        SandboxTemplateSpecPodTemplateSpecVolumes(
                            name=_BUILDBUDDY_SECRET,
                            secret=SandboxTemplateSpecPodTemplateSpecVolumesSecret(
                                secret_name=_BUILDBUDDY_SECRET,
                                default_mode=288,  # 0440; readable via fsGroup 1000
                            ),
                        )
                    ]
                    if self.buildbuddy_secret
                    else []
                ),
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
        spec = {"pod": pod, "stateVolume": state_volume}
        ApiObject(
            self,
            "sandboxtemplate",
            api_version=f"{GROUP}/{VERSION}",
            kind="SandboxTemplate",
            metadata=ApiObjectMetadata(name=name, namespace=self.env.namespace),
        ).add_json_patch(JsonPatch.add("/spec", spec))
        return spec
