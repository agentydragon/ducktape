"""VPA policies for selected static Deployments in Goldilocks-disabled namespaces."""

from cdk8s import ApiObjectMetadata, App, Chart
from vpa_crds.io.k8s.autoscaling import VerticalPodAutoscaler, VerticalPodAutoscalerSpecTargetRef

from cluster.cdk8s.manifest_roots import GENERATED_ROOT
from cluster.cdk8s.providers.vpa.policy import requests_only_spec

NAME = "static-application-vpa"
OUTPUT_DIR = f"{GENERATED_ROOT}/{NAME}"

# Only named Deployments are in scope. The namespace opt-outs also cover dynamic workloads.
_WORKLOADS = (
    ("agentplane-staging", "central-sampler", "central-sampler", "Off"),
    ("agentplane-staging", "egress", "proxy", "Initial"),
    ("haku-ci", "haku-ci-egress", "egress-sidecar", "Initial"),
)


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    for namespace, deployment, container, update_mode in _WORKLOADS:
        VerticalPodAutoscaler(
            chart,
            deployment,
            metadata=ApiObjectMetadata(name=deployment, namespace=namespace),
            spec=requests_only_spec(
                VerticalPodAutoscalerSpecTargetRef(api_version="apps/v1", kind="Deployment", name=deployment),
                container_name=container,
                update_mode=update_mode,
            ),
        )
    return chart
