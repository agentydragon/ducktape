"""trust-manager: its Namespace and HelmRelease, from cert-manager's jetstack HelmRepository.

`values` is an untyped dict: Helm values carry no schema for `cdk8s_import` to ingest.
"""

from __future__ import annotations

from cdk8s import App, Chart

from cluster.cdk8s import namespaces, node_scheduling
from cluster.cdk8s.cert_manager.app import JETSTACK_SOURCE_REF
from cluster.cdk8s.helm import RETRY_FAILED_INSTALL, helm_release
from cluster.cdk8s.manifest_roots import GENERATED_ROOT
from cluster.cdk8s.namespaces import Vpa, VpaProfile
from cluster.cdk8s.vpa_policy import ContainerPolicy, ControlledValues

NAME = "trust-manager"
NAMESPACE = "cert-manager-trust"
OUTPUT_DIR = f"{GENERATED_ROOT}/cert-manager/trust"


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    namespaces.namespace(
        chart,
        "namespace",
        name=NAMESPACE,
        vpa=VpaProfile(
            Vpa.INITIAL,
            (
                # Reserve above the webhook's 30-day peaks of 19.1m CPU and 65Mi working set.
                ContainerPolicy(
                    container_name=NAME,
                    controlled_values=ControlledValues.REQUESTS_ONLY,
                    controlled_resources=("cpu", "memory"),
                    min_allowed={"cpu": "25m", "memory": "128Mi"},
                    max_allowed={"cpu": "100m", "memory": "256Mi"},
                ),
                # The chart's trust package init container has its own startup behavior.
                ContainerPolicy(controlled_values=ControlledValues.REQUESTS_ONLY, mode="Off"),
            ),
        ),
    )
    helm_release(
        chart,
        NAME,
        NAMESPACE,
        repository=JETSTACK_SOURCE_REF,
        chart="trust-manager",
        version="0.25.*",
        interval="30m",
        install=RETRY_FAILED_INSTALL,
        values={
            "replicaCount": 1,
            # Backs a failurePolicy: Fail webhook on Bundle, so its absence rejects writes
            # rather than degrading. Same treatment as the other blocking-webhook backends.
            "priorityClassName": "system-cluster-critical",
            "tolerations": [node_scheduling.CONTROL_PLANE_TOLERATION],
        },
    )
    return chart
