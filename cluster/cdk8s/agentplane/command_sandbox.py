"""The sandbox Actions' own boxes, behind the egress path every agentplane box shares (sandbox_pod.py),
with no harness and no state volume. The command box runs the plain sandbox image
(agentplane/env/images/sandbox.nix) and costs the namespace quota what a command needs; the build box is
the same box sized for a build, on the build image (agentplane/env/images/build.nix), which adds Bazel
and its toolchain. Each is a core PodTemplate, not a SandboxTemplate: the Action Service creates
every box as a plain Pod from one, and staging.py offers both by name (`TEMPLATES`).
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from cdk8s import ApiObjectMetadata
from cdk8s_plus_34 import k8s
from cilium_crds.io.cilium import CiliumNetworkPolicySpecIngress
from constructs import Construct

from agentplane.egress import sidecar_config
from cluster.cdk8s import cilium, node_scheduling
from cluster.cdk8s.agentplane import egress, platform_routes, sandbox_pod
from cluster.cdk8s.agentplane.environment import Environment
from cluster.cdk8s.cert_manager import cluster_ca
from cluster.cdk8s.container_resources import container_resources
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
# The workload CA alone, on the sidecar: it verifies the proxy's and the platform routes' server SVIDs.
_SIDECAR_TRUST_VOLUME = "workload-trust"
_SIDECAR_TRUST_DIR = Path("/run/agentplane/workload-trust")
# The box's own SVID, which the Action Service mounts into the sidecar alone; keep in sync with
# `svidMountPath` in agentplane/action_service/sandbox/inventory.go.
_SVID_DIR = Path("/run/agentplane/workload-svid")
COMMAND_RESOURCES = container_resources(cpu="100m", memory="256Mi", cpu_limit="1", memory_limit="2Gi")
# A Bazel build of the system test suite fits in.
_BUILD_RESOURCES = container_resources(cpu="500m", memory="1Gi", cpu_limit="2", memory_limit="4Gi")
# The build box's home, on the Pod rather than in the container: running out of memory kills every
# process in the container, and the container that replaces it still has the checkout and Bazel's
# cache.
_BUILD_HOME = k8s.Volume(name="home", empty_dir=k8s.EmptyDirVolumeSource(size_limit=k8s.Quantity.from_string("20Gi")))


# What a box made from each template holds, for the agent choosing one.
TEMPLATES = {
    NAME: (
        "A box to run commands in: bash and coreutils, git, curl, ripgrep, jq, openssl, kubectl "
        "(Kubernetes access follows the caller Identity's reviewed egress rules) and python3 (install packages into a "
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
        runner_zone: str | None,
        image_pull_secret_name: str | None = sandbox_pod.SECRET_NAME,
        trust_mounts: Sequence[k8s.VolumeMount] | None = None,
    ) -> None:
        super().__init__(scope, id)
        self.env = env
        self.runner_zone = runner_zone
        self.image_pull_secret_name = image_pull_secret_name
        mounts = trust_mounts if trust_mounts is not None else sandbox_pod.proxy_trust_mounts()
        self.sidecar_settings = sandbox_pod.add_sidecar_settings(
            self,
            env,
            name=f"{NAME}-egress-sidecar",
            platform_routes=platform_routes.listener(platform_routes.egress_rules(env.namespace)),
            proxy_transport=sidecar_config.TLSProxyTransport(
                ca_file=_SIDECAR_TRUST_DIR / cluster_ca.BUNDLE_KEY,
                auth=sidecar_config.SpiffeProxyAuth(cert_file=_SVID_DIR / "tls.crt", key_file=_SVID_DIR / "tls.key"),
            ),
        )
        self.add_template(
            "podtemplate",
            metadata=k8s.ObjectMeta(name=NAME, namespace=env.namespace),
            workload=workload(_IMAGE, COMMAND_RESOURCES, env, trust_mounts=mounts),
        )
        self.add_template(
            "build-podtemplate",
            metadata=k8s.ObjectMeta(name=BUILD_NAME, namespace=env.namespace),
            workload=workload(
                _BUILD_IMAGE,
                _BUILD_RESOURCES,
                env,
                k8s.VolumeMount(name=_BUILD_HOME.name, mount_path=HOME),
                trust_mounts=mounts,
            ),
            volumes=[_BUILD_HOME],
        )

    def add_template(
        self, id: str, *, metadata: k8s.ObjectMeta, workload: k8s.Container, volumes: Sequence[k8s.Volume] = ()
    ) -> k8s.KubePodTemplate:
        """Add a command workload with this component's shared sidecar and credential wiring."""
        return k8s.KubePodTemplate(
            self,
            id,
            metadata=metadata,
            template=k8s.PodTemplateSpec(
                metadata=k8s.ObjectMeta(labels=_LABELS),
                # The Action Service runs each box as its caller, with no account of its own.
                spec=sandbox_pod.pod_spec(
                    self.env,
                    workload=workload,
                    workload_volumes=[
                        *volumes,
                        k8s.Volume(
                            name=_SIDECAR_TRUST_VOLUME,
                            config_map=k8s.ConfigMapVolumeSource(name=cluster_ca.WORKLOAD_BUNDLE),
                        ),
                    ],
                    sidecar_settings=self.sidecar_settings,
                    sidecar_mounts=[
                        k8s.VolumeMount(name=_SIDECAR_TRUST_VOLUME, mount_path=str(_SIDECAR_TRUST_DIR), read_only=True)
                    ],
                    image_pull_secret_name=self.image_pull_secret_name,
                    node_selector=(
                        {node_scheduling.ZONE_LABEL: self.runner_zone} if self.runner_zone is not None else None
                    ),
                ),
            ),
        )

    def add_platform_resources(self) -> None:
        """Limit command boxes to DNS and the fleet egress proxy."""
        env = self.env
        # DNS and the egress proxy out, nothing in: `exec` reaches a box through the API server.
        NetworkPolicy(
            self,
            "networkpolicy",
            metadata=ApiObjectMetadata(name=NAME, namespace=env.namespace),
            endpoint_selector=_LABELS,
            ingress=[CiliumNetworkPolicySpecIngress()],
            egress=[cilium.dns_egress(), egress.proxy(env.namespace).egress()],
        )


def workload(
    image: str,
    resources: k8s.ResourceRequirements,
    env: Environment,
    *mounts: k8s.VolumeMount,
    trust_mounts: Sequence[k8s.VolumeMount] = (),
    image_pull_policy: str | None = None,
) -> k8s.Container:
    return k8s.Container(
        name=CONTAINER,
        image=f"{image}:{_PLACEHOLDER_TAG}",
        image_pull_policy=image_pull_policy,
        # The image has no entrypoint of its own: the box idles until something is exec'd into it.
        # PID 1 ignores a signal it installs no handler for, so a bare `sleep` would hold every
        # dispose, and the quota the box holds, for the whole termination grace period.
        command=["bash", "-c", "trap 'exit 0' TERM; sleep infinity & wait"],
        security_context=sandbox_pod.workload_security_context(),
        env=sandbox_pod.workload_environment(
            env,
            [
                *sandbox_pod.proxy_environment(),
                k8s.EnvVar(name="AGENTPLANE_EGRESS_GRPC_URL", value=platform_routes.listener_url()),
            ],
        ),
        resources=resources,
        volume_mounts=[*trust_mounts, sandbox_pod.bazelrc_mount(), *mounts],
    )
