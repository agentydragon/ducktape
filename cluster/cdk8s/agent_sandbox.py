"""Install and configure the kubernetes-sigs/agent-sandbox controller from its pinned release."""

from __future__ import annotations

from cdk8s import App, Chart
from cdk8s_plus_34 import k8s

from cluster.cdk8s import namespaces
from cluster.cdk8s.container_resources import quantities
from cluster.cdk8s.manifest_roots import GENERATED_ROOT
from cluster.cdk8s.namespaces import Vpa, initial_request_annotations
from cluster.cdk8s.vpa_policy import ContainerPolicy, ControlledValues

CONTROLLER_DIR = f"{GENERATED_ROOT}/agents/agent-sandbox/controller"
VERSION = "v0.5.5"
RELEASE = f"https://github.com/kubernetes-sigs/agent-sandbox/releases/download/{VERSION}/sandbox-with-extensions.yaml"
# Names the release manifest gives its namespace and controller.
_NAMESPACE = "agent-sandbox-system"
_CONTROLLER = "agent-sandbox-controller"
_CPU_REQUEST = "15m"
_MEMORY_REQUEST = "128Mi"


def controller_patches(app: App) -> Chart:
    """Strategic-merge patches of the release: the namespace's labels, and the controller's
    placement, security context and health probes."""
    chart = Chart(app, "patches", disable_resource_name_hashes=True)
    namespaces.namespace_patch(chart, "namespace", name=_NAMESPACE, vpa=Vpa.RECOMMEND, labels={"name": _NAMESPACE})
    labels = {"app": _CONTROLLER}
    healthz = k8s.IntOrString.from_string("healthz")
    k8s.KubeDeployment(
        chart,
        "controller",
        metadata=k8s.ObjectMeta(
            name=_CONTROLLER,
            namespace=_NAMESPACE,
            annotations=initial_request_annotations(
                ContainerPolicy(
                    container_name=_CONTROLLER,
                    controlled_values=ControlledValues.REQUESTS_ONLY,
                    controlled_resources=("cpu", "memory"),
                    min_allowed={"cpu": _CPU_REQUEST, "memory": _MEMORY_REQUEST},
                )
            ),
        ),
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
                            image=f"registry.k8s.io/agent-sandbox/agent-sandbox-controller:{VERSION}",
                            # Reserve for bootstrap before Goldilocks has a recommendation.
                            # The 30-day sampled working-set peak was 74 MiB.
                            resources=k8s.ResourceRequirements(
                                requests=quantities({"cpu": _CPU_REQUEST, "memory": _MEMORY_REQUEST})
                            ),
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
