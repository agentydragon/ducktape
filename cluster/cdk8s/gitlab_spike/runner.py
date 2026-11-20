"""Project-scoped Kubernetes Runner; job pods have no Kubernetes credential."""

from cdk8s import ApiObjectMetadata, App, Chart
from cdk8s_plus_34 import ServiceAccount
from cilium_crds.io.cilium import CiliumNetworkPolicySpecEndpointSelector
from python.runfiles import Runfiles

from cluster.cdk8s import cilium, node_scheduling
from cluster.cdk8s.helm import helm_release, https_helm_repository
from cluster.cdk8s.namespaces import initial_memory_request_annotations
from cluster.cdk8s.providers.cilium.network_policy import EgressRule, Entity, NetworkPolicy
from cluster.cdk8s.vpa_policy import ContainerPolicy

NAME = "gitlab-spike-runner"
NAMESPACE = "gitlab-spike-ci"
_CPU_REQUEST = "100m"
_MEMORY_LIMIT = "512Mi"


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    ServiceAccount(
        chart,
        "job-account",
        metadata=ApiObjectMetadata(name="gitlab-spike-job", namespace=NAMESPACE),
        automount_token=False,
    )
    # The Helm chart uses this dedicated service account for the manager; jobs use a different one.
    # The manager alone needs the API. Build/helper pods cannot reach it.
    for name, selector, api_access in (
        ("manager", {"io.cilium.k8s.policy.serviceaccount": NAME}, True),
        ("jobs", {"app.kubernetes.io/name": "gitlab-spike-job"}, False),
    ):
        NetworkPolicy(
            chart,
            f"egress-{name}",
            metadata=ApiObjectMetadata(name=f"{NAME}-{name}", namespace=NAMESPACE),
            endpoint_selector=CiliumNetworkPolicySpecEndpointSelector(match_labels=selector),
            egress=[
                cilium.dns_egress(resolves=["*"]),
                EgressRule.to_endpoints({"k8s:io.kubernetes.pod.namespace": "gitlab-spike"}, 8181, 5000),
                cilium.egress_via_gateway("gitlab-spike.allegedly.works", "registry-gitlab-spike.allegedly.works"),
                *(
                    [EgressRule.to_entities(Entity.KUBE_APISERVER, ports=[443, 6443])]
                    if api_access
                    else [EgressRule.to_fqdns("cgr.dev", "9236a389bd48b984df91adc1bc924620.r2.cloudflarestorage.com")]
                ),
            ],
        )
    helm_release(
        chart,
        NAME,
        NAMESPACE,
        repository=https_helm_repository(chart, NAME, NAMESPACE, url="https://charts.gitlab.io"),
        chart="gitlab-runner",
        version="0.94.1",
        interval="15m",
        timeout="10m",
        values={
            "gitlabUrl": "http://gitlab-spike-webservice-default.gitlab-spike.svc:8181",
            "concurrent": 2,
            "checkInterval": 5,
            # Opt in the runner manager Deployment. Peak startup working set was 142Mi.
            "deploymentAnnotations": initial_memory_request_annotations(
                ContainerPolicy.memory_requests_only(
                    f"{NAME}-gitlab-runner", cpu_request=_CPU_REQUEST, memory_floor="160Mi", memory_limit=_MEMORY_LIMIT
                )
            ),
            "rbac": {"create": True, "clusterWideAccess": False},
            "serviceAccount": {"create": True, "name": NAME},
            "runners": {
                "secret": NAME,
                "config": (
                    Runfiles.CreateOrRaise().root() / "ducktape/cluster/cdk8s/gitlab_spike/runner.toml"
                ).read_text(),
            },
            "nodeSelector": node_scheduling.HIL_OVH_NODE_SELECTOR,
            "resources": {"requests": {"cpu": _CPU_REQUEST, "memory": "128Mi"}, "limits": {"memory": _MEMORY_LIMIT}},
            "securityContext": {
                "allowPrivilegeEscalation": False,
                "readOnlyRootFilesystem": False,
                "runAsNonRoot": True,
                "capabilities": {"drop": ["ALL"]},
            },
        },
    )
    return chart
