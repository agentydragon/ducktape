"""The roaming node controller: marks a roaming laptop node out of service while its kubelet
is silent (see //cluster/roaming_node_controller). Its RBAC, pull credential and the
CiliumNetworkPolicy confining its egress to the API server.

The image tag is the placeholder "unset"; the hand-written `PINS_DIR` Component overrides it
via Flux's image-automation marker.
"""

from __future__ import annotations

from cdk8s import ApiObjectMetadata, App, Chart
from cdk8s_plus_34 import k8s

from cluster.cdk8s import namespaces, node_scheduling, pod_policy
from cluster.cdk8s.container_resources import container_resources
from cluster.cdk8s.forgejo_registry.pull_credentials import SECRET_NAME, forgejo_images_creds_external_secret
from cluster.cdk8s.manifest_roots import GENERATED_ROOT, HAND_WRITTEN_ROOT
from cluster.cdk8s.namespaces import Vpa
from cluster.cdk8s.providers.cilium.network_policy import EgressRule, Entity, NetworkPolicy
from cluster.cdk8s.vpa_policy import ContainerPolicy

NAME = "roaming-node-controller"
OUTPUT_DIR = f"{GENERATED_ROOT}/roaming-node-controller"
PINS_DIR = f"{HAND_WRITTEN_ROOT}/roaming-node-controller-image-pins"
_IMAGE = "git.allegedly.works/ducktape-ci/roaming-node-controller:unset"
_LABELS = {"app.kubernetes.io/name": NAME}
# Where every kubelet renews the Lease the controller watches.
_NODE_LEASE_NAMESPACE = "kube-node-lease"
# The binary's health endpoint (`healthProbeAddress` in main.go).
_HEALTH_PORT = 8081


def _rbac(chart: Chart) -> None:
    subjects = [k8s.Subject(kind="ServiceAccount", name=NAME, namespace=NAME)]
    k8s.KubeServiceAccount(chart, "service-account", metadata=k8s.ObjectMeta(name=NAME, namespace=NAME))
    nodes = k8s.KubeClusterRole(
        chart,
        "node-tainter",
        metadata=k8s.ObjectMeta(name=NAME),
        rules=[k8s.PolicyRule(api_groups=[""], resources=["nodes"], verbs=["get", "list", "watch", "patch"])],
    )
    k8s.KubeClusterRoleBinding(
        chart,
        "node-tainter-binding",
        metadata=k8s.ObjectMeta(name=NAME),
        role_ref=k8s.RoleRef(api_group="rbac.authorization.k8s.io", kind=nodes.kind, name=nodes.name),
        subjects=subjects,
    )
    leases = k8s.KubeRole(
        chart,
        "lease-reader",
        metadata=k8s.ObjectMeta(name=NAME, namespace=_NODE_LEASE_NAMESPACE),
        rules=[
            k8s.PolicyRule(api_groups=["coordination.k8s.io"], resources=["leases"], verbs=["get", "list", "watch"])
        ],
    )
    k8s.KubeRoleBinding(
        chart,
        "lease-reader-binding",
        metadata=k8s.ObjectMeta(name=NAME, namespace=_NODE_LEASE_NAMESPACE),
        role_ref=k8s.RoleRef(api_group="rbac.authorization.k8s.io", kind=leases.kind, name=leases.name),
        subjects=subjects,
    )


def _health_probe(*, initial_delay_seconds: int) -> k8s.Probe:
    return k8s.Probe(
        http_get=k8s.HttpGetAction(path="/healthz", port=k8s.IntOrString.from_number(_HEALTH_PORT)),
        initial_delay_seconds=initial_delay_seconds,
        period_seconds=20,
    )


def _deployment(chart: Chart) -> None:
    deployment = k8s.KubeDeployment(
        chart,
        "deployment",
        metadata=k8s.ObjectMeta(
            name=NAME,
            namespace=NAME,
            labels=_LABELS,
            annotations={
                "description": (
                    "Taints a roaming node node.kubernetes.io/out-of-service while its kubelet Lease is"
                    " stale, so DaemonSets and pod GC treat the absent laptop as gone; untaints on the"
                    " first renewal."
                ),
                **namespaces.initial_memory_request_annotations(
                    ContainerPolicy.memory_requests_only(
                        "controller", cpu_request="10m", memory_floor="64Mi", memory_limit="80Mi"
                    )
                ),
            },
        ),
        spec=k8s.DeploymentSpec(
            replicas=1,
            # No leader election: never two controllers at once.
            strategy=k8s.DeploymentStrategy(type="Recreate"),
            selector=k8s.LabelSelector(match_labels=_LABELS),
            template=k8s.PodTemplateSpec(
                metadata=k8s.ObjectMeta(labels=_LABELS),
                spec=k8s.PodSpec(
                    service_account_name=NAME,
                    automount_service_account_token=True,
                    image_pull_secrets=[k8s.LocalObjectReference(name=SECRET_NAME)],
                    security_context=k8s.PodSecurityContext(
                        run_as_non_root=True,
                        run_as_user=1000,
                        run_as_group=1000,
                        seccomp_profile=k8s.SeccompProfile(type="RuntimeDefault"),
                    ),
                    containers=[
                        k8s.Container(
                            name="controller",
                            image=_IMAGE,
                            security_context=k8s.SecurityContext(
                                allow_privilege_escalation=False,
                                read_only_root_filesystem=True,
                                capabilities=k8s.Capabilities(drop=["ALL"]),
                            ),
                            resources=container_resources(cpu="10m", memory="32Mi", memory_limit="128Mi"),
                            readiness_probe=_health_probe(initial_delay_seconds=3),
                            liveness_probe=_health_probe(initial_delay_seconds=10),
                        )
                    ],
                ),
            ),
        ),
    )
    # Runs where the laptops never are; must not itself depend on a roaming node.
    pod_policy.place(deployment, node_scheduling.HIL_OVH, tolerate_control_plane=True)


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    namespaces.namespace(chart, "namespace", name=NAME, vpa=Vpa.RECOMMEND)
    forgejo_images_creds_external_secret(chart, "forgejo-images-creds", namespace=NAME)
    _rbac(chart)
    _deployment(chart)
    NetworkPolicy(
        chart,
        "cilium-network-policy",
        metadata=ApiObjectMetadata(
            name=NAME,
            namespace=NAME,
            # In-cluster config addresses the API server by IP, so no DNS rule.
            annotations={"description": "Egress only to the kube-apiserver."},
        ),
        endpoint_selector=_LABELS,
        egress=[EgressRule.to_entities(Entity.KUBE_APISERVER, ports=[443, 6443])],
    )
    return chart
