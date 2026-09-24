"""The sandbox Actions' own box: the plain sandbox image (agentplane/sandbox_image/default.nix) behind
the egress path every agentplane box shares (sandbox_pod.py), with no harness and no state volume, so
it costs the namespace quota what a command needs. staging.py offers it as the sandbox group's
default environment.
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
)
from cilium_crds.io.cilium import CiliumNetworkPolicySpecIngress
from constructs import Construct

from cluster.cdk8s import cilium
from cluster.cdk8s.agentplane import egress, sandbox_pod
from cluster.cdk8s.agentplane.environment import Environment
from cluster.cdk8s.metadata import metadata

# The SandboxTemplate, its Pods' name label, and their fence. The egress proxy's policy spells it
# too (egress.py), since this module imports that one.
NAME = "agentplane-sandbox"
# The workload container, which `exec` runs commands in.
CONTAINER = "sandbox"
# The image's HOME and WorkingDir, writable by its uid 1000 (agentplane/sandbox_image/default.nix).
HOME = "/home/runner"
_IMAGE = "git.allegedly.works/ducktape-ci/agentplane-sandbox"
_PLACEHOLDER_TAG = "unset"  # always overridden by image-pins/kustomization.yaml
_LABELS = {"app.kubernetes.io/name": NAME}


class CommandSandbox(Construct):
    def __init__(self, scope: Construct, id: str, env: Environment) -> None:
        super().__init__(scope, id)
        namespace = env.namespace
        SandboxTemplate(
            self,
            "sandboxtemplate",
            metadata=metadata(NAME, namespace),
            spec=SandboxTemplateSpec(
                # The CiliumNetworkPolicy below is the box's fence.
                network_policy_management=SandboxTemplateSpecNetworkPolicyManagement.UNMANAGED,
                pod_template=SandboxTemplateSpecPodTemplate(
                    metadata=SandboxTemplateSpecPodTemplateMetadata(labels=_LABELS),
                    # No account of its own: the sandbox Actions stamp every box as its caller.
                    spec=sandbox_pod.pod_spec(env, workload=_workload(), service_account_name=None),
                ),
            ),
        )
        # DNS and the egress proxy out, nothing in: `exec` reaches the box through the API server.
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


def _workload() -> SandboxTemplateSpecPodTemplateSpecContainers:
    return SandboxTemplateSpecPodTemplateSpecContainers(
        name=CONTAINER,
        image=f"{_IMAGE}:{_PLACEHOLDER_TAG}",
        # The image has no entrypoint of its own: the box idles until something is exec'd into it.
        # PID 1 ignores a signal it installs no handler for, so a bare `sleep` would hold every
        # dispose, and the quota the box holds, for the whole termination grace period.
        command=["bash", "-c", "trap 'exit 0' TERM; sleep infinity & wait"],
        security_context=sandbox_pod.workload_security_context(),
        env=sandbox_pod.egress_env(),
        resources=SandboxTemplateSpecPodTemplateSpecContainersResources(
            requests={
                "cpu": SandboxTemplateSpecPodTemplateSpecContainersResourcesRequests.from_string("100m"),
                "memory": SandboxTemplateSpecPodTemplateSpecContainersResourcesRequests.from_string("256Mi"),
            },
            limits={
                "cpu": SandboxTemplateSpecPodTemplateSpecContainersResourcesLimits.from_string("1"),
                "memory": SandboxTemplateSpecPodTemplateSpecContainersResourcesLimits.from_string("2Gi"),
            },
        ),
        volume_mounts=[sandbox_pod.egress_ca_mount()],
    )
