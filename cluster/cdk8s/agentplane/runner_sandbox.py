"""The runner's boxes, behind the egress path every agentplane box shares (sandbox_pod.py): the
runner SandboxTemplate, which adds the harness runner and a state volume, and the ServiceAccount and
fence every runner template's Pods share. staging.py adds a second template on the ducktape image.

The templates' container images carry the same `unset` placeholder tag as the app's Deployment:
kustomize's `images:` transformer patches by image name across every resource in the Kustomization,
so image-pins/ covers them too.
"""

from __future__ import annotations

from collections.abc import Sequence

from agent_sandbox_sandboxtemplate_crds.io.x_k8s.agents.extensions import (
    SandboxTemplateSpecNetworkPolicyManagement,
    SandboxTemplateSpecPodTemplate,
    SandboxTemplateSpecPodTemplateMetadata,
    SandboxTemplateSpecPodTemplateSpecContainers,
    SandboxTemplateSpecPodTemplateSpecContainersEnv,
    SandboxTemplateSpecPodTemplateSpecContainersResources,
    SandboxTemplateSpecPodTemplateSpecContainersResourcesLimits,
    SandboxTemplateSpecPodTemplateSpecContainersResourcesRequests,
    SandboxTemplateSpecPodTemplateSpecContainersVolumeMounts,
    SandboxTemplateSpecPodTemplateSpecVolumes,
    SandboxTemplateSpecPodTemplateSpecVolumesSecret,
    SandboxTemplateSpecVolumeClaimTemplates,
    SandboxTemplateSpecVolumeClaimTemplatesMetadata,
    SandboxTemplateSpecVolumeClaimTemplatesPolicy,
    SandboxTemplateSpecVolumeClaimTemplatesSpec,
    SandboxTemplateSpecVolumeClaimTemplatesSpecResources,
    SandboxTemplateSpecVolumeClaimTemplatesSpecResourcesRequests,
)
from cdk8s import ApiObjectMetadata
from cdk8s_plus_34 import ServiceAccount
from constructs import Construct

from agentplane.action_service.sandbox.binding import DESCRIPTION_ANNOTATION
from cluster.cdk8s import cilium
from cluster.cdk8s.agentplane import egress, llm_ingress, sandbox_pod
from cluster.cdk8s.agentplane.environment import Environment
from cluster.cdk8s.providers.agent_sandbox.sandbox_template import SandboxTemplate
from cluster.cdk8s.providers.cilium.network_policy import NetworkPolicy, isolate_ingress
from cluster.cdk8s.service_ref import Pods

# Every runner template's Pod name label, and the runners' ServiceAccount and fence.
NAME = "agentplane-runner"
_IMAGE = "git.allegedly.works/ducktape-ci/runner"
_PLACEHOLDER_TAG = "unset"  # always overridden by image-pins/kustomization.yaml
_LABELS = {"app.kubernetes.io/name": NAME}
# Shared by the runner's --state-dir flag, its container volumeMount, and the
# SandboxTemplate's own VolumeClaimTemplate -- all three must name the same volume.
_STATE_VOLUME_NAME = "state"
_STATE_DIR = "/state"
_BUILDBUDDY_SECRET = "buildbuddy-api-key"
_BUILDBUDDY_MOUNT = "/run/buildbuddy"


def pods(namespace: str) -> Pods:
    """Every runner Pod in one environment's namespace, whichever template it was stamped from."""
    return Pods(namespace=namespace, labels=tuple(_LABELS.items()))


class RunnerSandbox(Construct):
    """The runners' ServiceAccount and fence, and the shared runner template."""

    def __init__(self, scope: Construct, id: str, env: Environment) -> None:
        super().__init__(scope, id)
        namespace = env.namespace
        # The runner Pods' identity, with no RBAC of its own.
        ServiceAccount(
            self, "serviceaccount", metadata=ApiObjectMetadata(name=NAME, namespace=namespace), automount_token=False
        )
        # Runner Pods accept no connections; they reach DNS and the egress proxy's listener, which the
        # sidecar relays to, and dial the Runner Gateway through that proxy.
        NetworkPolicy(
            self,
            "networkpolicy",
            metadata=ApiObjectMetadata(name=NAME, namespace=namespace),
            endpoint_selector=pods(namespace).selector,
            ingress=isolate_ingress(),
            egress=[cilium.dns_egress(), egress.proxy(namespace).egress()],
        )
        RunnerTemplate(
            self,
            "runner-template",
            env,
            name="runner",
            image=_IMAGE,
            description=(
                "The shared runner image, built to host an agent harness: the sandbox tools (git, "
                "curl, ripgrep, jq, openssl, kubectl, python3) plus the runner, Claude Code and Codex."
            ),
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
        description: str,
        buildbuddy_secret: bool = False,
        extra_harness_env: Sequence[str] = (),
    ) -> None:
        super().__init__(scope, id)
        self.env = env
        self.buildbuddy_secret = buildbuddy_secret
        self.extra_harness_env = extra_harness_env
        self._add_sandbox_template(name=name, image=image, description=description)

    def _runner_container(self, image: str) -> SandboxTemplateSpecPodTemplateSpecContainers:
        # Through the egress proxy, which matches the host on the exact string its policy names.
        llm = llm_ingress.service(self.env.namespace)
        litellm_url = f"http://{llm.fqdn}:{llm.port.number}"
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
                    SandboxTemplateSpecPodTemplateSpecContainersEnv(name="LITELLM_URL", value=litellm_url),
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

    def _add_sandbox_template(self, *, name: str, image: str, description: str) -> None:
        namespace = self.env.namespace
        SandboxTemplate(
            self,
            "sandboxtemplate",
            metadata=ApiObjectMetadata(
                name=name,
                namespace=namespace,
                # What the sandbox Actions tell an agent choosing among the templates they offer.
                annotations={DESCRIPTION_ANNOTATION: description},
            ),
            # The CiliumNetworkPolicy next to this construct is the runner's fence.
            network_policy_management=SandboxTemplateSpecNetworkPolicyManagement.UNMANAGED,
            volume_claim_templates_policy=SandboxTemplateSpecVolumeClaimTemplatesPolicy.OVERRIDES,
            pod_template=SandboxTemplateSpecPodTemplate(
                metadata=SandboxTemplateSpecPodTemplateMetadata(labels=_LABELS),
                spec=sandbox_pod.pod_spec(
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
                ),
            ),
            volume_claim_templates=[
                SandboxTemplateSpecVolumeClaimTemplates(
                    metadata=SandboxTemplateSpecVolumeClaimTemplatesMetadata(name=_STATE_VOLUME_NAME),
                    spec=SandboxTemplateSpecVolumeClaimTemplatesSpec(
                        # The bulk tier, and the only one a sandbox can have: OVH's
                        # `tier=ssd` nodes are control-plane, and a sandbox is not
                        # getting that toleration. Node-local, so a sandbox does
                        # not outlive its node -- which is what a sandbox is for.
                        storage_class_name="local-path-ovh-hdd",
                        access_modes=["ReadWriteOnce"],
                        resources=SandboxTemplateSpecVolumeClaimTemplatesSpecResources(
                            requests={
                                "storage": SandboxTemplateSpecVolumeClaimTemplatesSpecResourcesRequests.from_string(
                                    "10Gi"
                                )
                            }
                        ),
                    ),
                )
            ],
        )
