"""Install and configure the kubernetes-sigs/agent-sandbox controller from its pinned release."""

from __future__ import annotations

from cdk8s import App, Chart
from cdk8s_plus_34 import k8s

from cluster.cdk8s import namespaces
from cluster.cdk8s.flux import Kustomization, RenderedDirectory, flux_kustomization
from cluster.cdk8s.manifest_roots import GENERATED_ROOT
from cluster.cdk8s.namespaces import Vpa

CONTROLLER_DIR = f"{GENERATED_ROOT}/agents/agent-sandbox/controller"
_VERSION = "v0.5.5"
# MODULE.bazel's `agent_sandbox_release_bundle` pins the same asset, by SHA-256, for the CRD
# bindings; keep the URLs equal.
RELEASE = f"https://github.com/kubernetes-sigs/agent-sandbox/releases/download/{_VERSION}/sandbox-with-extensions.yaml"
# Names the release manifest gives its namespace and controller.
_NAMESPACE = "agent-sandbox-system"
_CONTROLLER = "agent-sandbox-controller"


def controller_patches(app: App) -> Chart:
    """Strategic-merge patches of the release: the namespace's labels, and the controller's
    placement, security context and health probes."""
    chart = Chart(app, "patches", disable_resource_name_hashes=True)
    namespaces.namespace_patch(chart, "namespace", name=_NAMESPACE, vpa=Vpa.DISABLED, labels={"name": _NAMESPACE})
    labels = {"app": _CONTROLLER}
    healthz = k8s.IntOrString.from_string("healthz")
    k8s.KubeDeployment(
        chart,
        "controller",
        metadata=k8s.ObjectMeta(name=_CONTROLLER, namespace=_NAMESPACE),
        spec=k8s.DeploymentSpec(
            replicas=1,
            selector=k8s.LabelSelector(match_labels=labels),
            template=k8s.PodTemplateSpec(
                metadata=k8s.ObjectMeta(labels=labels),
                spec=k8s.PodSpec(
                    # Keep the controller and the workspaces on always-on OVH capacity.
                    node_selector={"topology.kubernetes.io/region": "hil"},
                    security_context=k8s.PodSecurityContext(
                        run_as_non_root=True, seccomp_profile=k8s.SeccompProfile(type="RuntimeDefault")
                    ),
                    containers=[
                        k8s.Container(
                            name=_CONTROLLER,
                            image=f"registry.k8s.io/agent-sandbox/agent-sandbox-controller:{_VERSION}",
                            security_context=k8s.SecurityContext(
                                allow_privilege_escalation=False, capabilities=k8s.Capabilities(drop=["ALL"])
                            ),
                            liveness_probe=k8s.Probe(
                                http_get=k8s.HttpGetAction(path="/healthz", port=healthz),
                                initial_delay_seconds=15,
                                period_seconds=20,
                            ),
                            readiness_probe=k8s.Probe(
                                http_get=k8s.HttpGetAction(path="/readyz", port=healthz),
                                initial_delay_seconds=5,
                                period_seconds=10,
                            ),
                        )
                    ],
                ),
            ),
        ),
    )
    return chart


def agent_sandbox_controller(chart: Chart, directory: RenderedDirectory) -> Kustomization:
    return flux_kustomization(
        chart,
        "agent-sandbox-controller",
        directory,
        timeout="5m",
        description=(
            f"kubernetes-sigs/agent-sandbox {_VERSION} combined release asset "
            "(Sandbox, SandboxTemplate, SandboxClaim, SandboxWarmPool CRDs)."
        ),
    )
