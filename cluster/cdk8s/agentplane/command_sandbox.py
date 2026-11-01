"""The sandbox Actions' own boxes, behind the egress path every agentplane box shares (sandbox_pod.py),
with no harness and no state volume. The command box runs the plain sandbox image
(agentplane/env/images/sandbox.nix) and costs the namespace quota what a command needs; the build box is
the same box sized for a build, on the build image (agentplane/env/images/build.nix), which adds Bazel
and its toolchain. Each is a core PodTemplate, not a SandboxTemplate: the Action Service creates
every box as a plain Pod from one, and staging.py offers both by name (`TEMPLATES`).
"""

from __future__ import annotations

from collections.abc import Sequence

from cdk8s import ApiObject, ApiObjectMetadata, JsonPatch
from cdk8s_plus_34 import k8s
from cilium_crds.io.cilium import CiliumNetworkPolicySpecIngress
from constructs import Construct

from cluster.cdk8s import cilium, node_scheduling
from cluster.cdk8s.agentplane import egress, sandbox_pod
from cluster.cdk8s.agentplane.environment import Environment
from cluster.cdk8s.providers.cilium.network_policy import NetworkPolicy

# The command box's PodTemplate, and both boxes' Pod name label and fence. The egress proxy's
# policy spells it too (egress.py), since this module imports that one.
NAME = "sandbox"
BUILD_NAME = "sandbox-build"
# The workload container, which `exec` runs commands in.
CONTAINER = "sandbox"
# The image's HOME and WorkingDir, writable by its uid 1000 (agentplane/env/images/sandbox.nix).
HOME = "/home/runner"
_IMAGE = "git.allegedly.works/ducktape-ci/agentplane-sandbox"
_BUILD_IMAGE = "git.allegedly.works/ducktape-ci/agentplane-sandbox-build"
_PLACEHOLDER_TAG = "unset"  # always overridden by image-pins/kustomization.yaml
_LABELS = {"app.kubernetes.io/name": NAME}
_COMMAND_RESOURCES = k8s.ResourceRequirements(
    requests={"cpu": k8s.Quantity.from_string("100m"), "memory": k8s.Quantity.from_string("256Mi")},
    limits={"cpu": k8s.Quantity.from_string("1"), "memory": k8s.Quantity.from_string("2Gi")},
)
# A Bazel build of the system test suite fits in.
_BUILD_RESOURCES = k8s.ResourceRequirements(
    requests={"cpu": k8s.Quantity.from_string("500m"), "memory": k8s.Quantity.from_string("1Gi")},
    limits={"cpu": k8s.Quantity.from_string("2"), "memory": k8s.Quantity.from_string("4Gi")},
)
# The build box's home, on the Pod rather than in the container: running out of memory kills every
# process in the container, and the container that replaces it still has the checkout and Bazel's
# cache.
_BUILD_HOME = k8s.Volume(name="home", empty_dir=k8s.EmptyDirVolumeSource(size_limit=k8s.Quantity.from_string("20Gi")))


# What a box made from each template holds, for the agent choosing one.
TEMPLATES = {
    NAME: (
        "A box to run commands in: bash and coreutils, git, curl, ripgrep, jq, openssl, kubectl "
        "(configured as the caller's ServiceAccount) and python3 (install packages into a "
        "`python3 -m venv`). 1 core and 2Gi, and no volume: files last as long as the box's Pod."
    ),
    BUILD_NAME: (
        "The sandbox box sized for a build: the same tools plus bazel (bazelisk, which runs the "
        "version a workspace's `.bazelversion` names) and gcc, 2 cores and 4Gi, and a home directory "
        "that survives the container being killed for running out of memory, though not the box's Pod."
    ),
}


class CommandSandbox(Construct):
    def __init__(
        self,
        scope: Construct,
        id: str,
        env: Environment,
        *,
        image_pull_secret_name: str | None = sandbox_pod.SECRET_NAME,
        sidecar_resources: k8s.ResourceRequirements | None = None,
        trust_mounts: Sequence[k8s.VolumeMount] | None = None,
    ) -> None:
        super().__init__(scope, id)
        self.image_pull_secret_name = image_pull_secret_name
        self.sidecar_resources = sidecar_resources
        mounts = trust_mounts if trust_mounts is not None else sandbox_pod.egress_mounts()
        self.sidecar_settings = sandbox_pod.add_sidecar_settings(
            self, env, name=f"{NAME}-egress-sidecar", platform_routes={}
        )
        _pod_template(
            self,
            "podtemplate",
            env,
            name=NAME,
            workload=_workload(_IMAGE, _COMMAND_RESOURCES, env, trust_mounts=mounts),
        )
        _pod_template(
            self,
            "build-podtemplate",
            env,
            name=BUILD_NAME,
            workload=_workload(
                _BUILD_IMAGE,
                _BUILD_RESOURCES,
                env,
                k8s.VolumeMount(name=_BUILD_HOME.name, mount_path=HOME),
                trust_mounts=mounts,
            ),
            volumes=[_BUILD_HOME],
        )
        self.env = env

    def add_platform_resources(self) -> None:
        """Limit command boxes to DNS and the fleet egress proxy."""
        env = self.env
        # DNS and the egress proxy out, nothing in: `exec` reaches a box through the API server.
        NetworkPolicy(
            self,
            "networkpolicy",
            metadata=ApiObjectMetadata(name=NAME.removeprefix("agentplane-"), namespace=env.namespace),
            endpoint_selector=_LABELS,
            ingress=[CiliumNetworkPolicySpecIngress()],
            egress=[cilium.dns_egress(), egress.proxy(env.namespace).egress()],
        )


def _pod_template(
    scope: CommandSandbox,
    id: str,
    env: Environment,
    *,
    name: str,
    workload: k8s.Container,
    volumes: Sequence[k8s.Volume] = (),
) -> None:
    rendered = sandbox_pod.render_pod_template(
        _LABELS,
        # No account of its own: the sandbox Actions run every box as its caller
        # (agentplane/action_service/sandbox/inventory.go).
        sandbox_pod.pod_spec(
            env,
            workload=workload,
            workload_volumes=volumes,
            sidecar_settings=scope.sidecar_settings,
            sidecar_resources=scope.sidecar_resources,
            image_pull_secret_name=scope.image_pull_secret_name,
            # Keep command boxes in the environment's runner zone alongside LiteLLM.
            node_selector=(
                {node_scheduling.ZONE_LABEL: env.app.runner_zone} if env.app.runner_zone is not None else None
            ),
        ),
    )
    # A PodTemplate rather than a Pod spec in the Action Service's ConfigMap: the image pins
    # (image-pins/kustomization.yaml) rewrite containers in any object, never inside a string.
    ApiObject(
        scope, id, api_version="v1", kind="PodTemplate", metadata=ApiObjectMetadata(name=name, namespace=env.namespace)
    ).add_json_patch(JsonPatch.add("/template", rendered))


def _workload(
    image: str,
    resources: k8s.ResourceRequirements,
    env: Environment,
    *mounts: k8s.VolumeMount,
    trust_mounts: Sequence[k8s.VolumeMount] = (),
) -> k8s.Container:
    return k8s.Container(
        name=CONTAINER,
        image=f"{image}:{_PLACEHOLDER_TAG}",
        # The image has no entrypoint of its own: the box idles until something is exec'd into it.
        # PID 1 ignores a signal it installs no handler for, so a bare `sleep` would hold every
        # dispose, and the quota the box holds, for the whole termination grace period.
        command=["bash", "-c", "trap 'exit 0' TERM; sleep infinity & wait"],
        security_context=sandbox_pod.workload_security_context(),
        env=sandbox_pod.workload_environment(env, sandbox_pod.egress_env()),
        resources=resources,
        volume_mounts=[*trust_mounts, sandbox_pod.bazelrc_mount(), *mounts],
    )
