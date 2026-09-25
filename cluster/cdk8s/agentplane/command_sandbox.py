"""The sandbox Actions' own boxes: the plain sandbox image (agentplane/images/sandbox.nix) behind
the egress path every agentplane box shares (sandbox_pod.py), with no harness and no state volume.
The command box costs the namespace quota what a command needs; the build box is the same box sized
for a build. staging.py offers both to the sandbox Actions, the command box as the default, and each
template's `description` annotation is what those Actions tell an agent choosing one.
"""

from __future__ import annotations

from agent_sandbox_sandboxtemplate_crds.io.x_k8s.agents.extensions import (
    SandboxTemplate,
    SandboxTemplateSpec,
    SandboxTemplateSpecNetworkPolicyManagement,
    SandboxTemplateSpecPodTemplate,
    SandboxTemplateSpecPodTemplateMetadata,
    SandboxTemplateSpecPodTemplateSpecContainers,
    SandboxTemplateSpecPodTemplateSpecContainersResources,
    SandboxTemplateSpecPodTemplateSpecContainersResourcesLimits,
    SandboxTemplateSpecPodTemplateSpecContainersResourcesRequests,
    SandboxTemplateSpecPodTemplateSpecContainersVolumeMounts,
    SandboxTemplateSpecPodTemplateSpecVolumes,
    SandboxTemplateSpecPodTemplateSpecVolumesEmptyDir,
    SandboxTemplateSpecPodTemplateSpecVolumesEmptyDirSizeLimit,
)
from cilium_crds.io.cilium import CiliumNetworkPolicySpecIngress
from constructs import Construct

from cluster.cdk8s import cilium
from cluster.cdk8s.agentplane import egress, sandbox_pod
from cluster.cdk8s.agentplane.environment import Environment
from cluster.cdk8s.metadata import metadata

# The command box's SandboxTemplate, and both boxes' Pod name label and fence. The egress proxy's
# policy spells it too (egress.py), since this module imports that one.
NAME = "agentplane-sandbox"
BUILD_NAME = "agentplane-sandbox-build"
# The workload container, which `exec` runs commands in.
CONTAINER = "sandbox"
# The image's HOME and WorkingDir, writable by its uid 1000 (agentplane/images/sandbox.nix).
HOME = "/home/runner"
_IMAGE = "git.allegedly.works/ducktape-ci/agentplane-sandbox"
_PLACEHOLDER_TAG = "unset"  # always overridden by image-pins/kustomization.yaml
_LABELS = {"app.kubernetes.io/name": NAME}
_COMMAND_RESOURCES = SandboxTemplateSpecPodTemplateSpecContainersResources(
    requests={
        "cpu": SandboxTemplateSpecPodTemplateSpecContainersResourcesRequests.from_string("100m"),
        "memory": SandboxTemplateSpecPodTemplateSpecContainersResourcesRequests.from_string("256Mi"),
    },
    limits={
        "cpu": SandboxTemplateSpecPodTemplateSpecContainersResourcesLimits.from_string("1"),
        "memory": SandboxTemplateSpecPodTemplateSpecContainersResourcesLimits.from_string("2Gi"),
    },
)
# The most the LimitRange lets one container have (rbac.py), which a Bazel build of the acceptance
# suite fit in.
_BUILD_RESOURCES = SandboxTemplateSpecPodTemplateSpecContainersResources(
    requests={
        "cpu": SandboxTemplateSpecPodTemplateSpecContainersResourcesRequests.from_string("500m"),
        "memory": SandboxTemplateSpecPodTemplateSpecContainersResourcesRequests.from_string("1Gi"),
    },
    limits={
        "cpu": SandboxTemplateSpecPodTemplateSpecContainersResourcesLimits.from_string("2"),
        "memory": SandboxTemplateSpecPodTemplateSpecContainersResourcesLimits.from_string("4Gi"),
    },
)
# The build box's home, on the Pod rather than in the container: running out of memory kills every
# process in the container, and the container that replaces it still has the checkout and Bazel's
# cache.
_BUILD_HOME = SandboxTemplateSpecPodTemplateSpecVolumes(
    name="home",
    empty_dir=SandboxTemplateSpecPodTemplateSpecVolumesEmptyDir(
        size_limit=SandboxTemplateSpecPodTemplateSpecVolumesEmptyDirSizeLimit.from_string("20Gi")
    ),
)


class CommandSandbox(Construct):
    def __init__(self, scope: Construct, id: str, env: Environment) -> None:
        super().__init__(scope, id)
        namespace = env.namespace
        _template(
            self,
            "sandboxtemplate",
            env,
            name=NAME,
            description=(
                "A box to run commands in: bash and coreutils, git, curl, ripgrep, jq, openssl, kubectl "
                "(configured as the caller's ServiceAccount) and python3 (install packages into a "
                "`python3 -m venv`). 1 core and 2Gi, and no volume: files last as long as the box's Pod."
            ),
            workload=_workload(_COMMAND_RESOURCES),
            volumes=[],
        )
        _template(
            self,
            "build-sandboxtemplate",
            env,
            name=BUILD_NAME,
            description=(
                "The sandbox box sized for a build: the same tools, 2 cores and 4Gi, and a home directory "
                "that survives the container being killed for running out of memory, though not the box's Pod."
            ),
            workload=_workload(
                _BUILD_RESOURCES,
                SandboxTemplateSpecPodTemplateSpecContainersVolumeMounts(name=_BUILD_HOME.name, mount_path=HOME),
            ),
            volumes=[_BUILD_HOME],
        )
        # DNS and the egress proxy out, nothing in: `exec` reaches a box through the API server.
        cilium.network_policy(
            self,
            "networkpolicy",
            metadata=metadata(NAME, namespace),
            selector=_LABELS,
            ingress=[CiliumNetworkPolicySpecIngress()],
            egress=[
                cilium.dns_egress(),
                cilium.egress_to(cilium.endpoint_labels(namespace, egress.NAME), egress.PROXY_PORT),
            ],
        )


def _template(
    scope: Construct,
    id: str,
    env: Environment,
    *,
    name: str,
    description: str,
    workload: SandboxTemplateSpecPodTemplateSpecContainers,
    volumes: list[SandboxTemplateSpecPodTemplateSpecVolumes],
) -> None:
    SandboxTemplate(
        scope,
        id,
        metadata=metadata(name, env.namespace, annotations={"description": description}),
        spec=SandboxTemplateSpec(
            # The CiliumNetworkPolicy beside it is the box's fence.
            network_policy_management=SandboxTemplateSpecNetworkPolicyManagement.UNMANAGED,
            pod_template=SandboxTemplateSpecPodTemplate(
                metadata=SandboxTemplateSpecPodTemplateMetadata(labels=_LABELS),
                # No account of its own: the sandbox Actions stamp every box as its caller.
                spec=sandbox_pod.pod_spec(env, workload=workload, service_account_name=None, workload_volumes=volumes),
            ),
        ),
    )


def _workload(
    resources: SandboxTemplateSpecPodTemplateSpecContainersResources,
    *mounts: SandboxTemplateSpecPodTemplateSpecContainersVolumeMounts,
) -> SandboxTemplateSpecPodTemplateSpecContainers:
    return SandboxTemplateSpecPodTemplateSpecContainers(
        name=CONTAINER,
        image=f"{_IMAGE}:{_PLACEHOLDER_TAG}",
        # The image has no entrypoint of its own: the box idles until something is exec'd into it.
        # PID 1 ignores a signal it installs no handler for, so a bare `sleep` would hold every
        # dispose, and the quota the box holds, for the whole termination grace period.
        command=["bash", "-c", "trap 'exit 0' TERM; sleep infinity & wait"],
        security_context=sandbox_pod.workload_security_context(),
        env=sandbox_pod.egress_env(),
        resources=resources,
        volume_mounts=[*sandbox_pod.egress_mounts(), sandbox_pod.bazelrc_mount(), *mounts],
    )
