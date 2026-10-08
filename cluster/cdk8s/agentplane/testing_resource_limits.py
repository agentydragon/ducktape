"""ResourceQuota and LimitRange used by the Agentplane testing environment."""

from __future__ import annotations

from cdk8s_plus_34 import k8s
from constructs import Construct


class TestingNamespaceResourceLimits(Construct):
    """The ResourceQuota and LimitRange applied by the testing environment only."""

    def __init__(self, scope: Construct, id: str, namespace: str) -> None:
        super().__init__(scope, id)
        # Bounds what runner sandboxes take from the node. Each costs 2100m of limits.cpu
        # (2 for the runner, 100m for the egress sidecar), about 4.1Gi of limits.memory
        # and a 10Gi state PVC, and the namespace's own service Pods count against the
        # same totals. Sized for those services plus four sandboxes at once, with room
        # left for a rollout's surge Pods; for more headroom, raise the limits, not a count.
        #
        # Aggregate resources only. A cap per object kind bounds an untrusted creator,
        # and only Flux and the integration app create objects here.
        #
        # The LimitRange supplies defaults for containers that omit them (the CNPG
        # postgres container declares none); its mutations are applied before quota validation.
        k8s.KubeResourceQuota(
            self,
            "resourcequota",
            metadata=k8s.ObjectMeta(name="quota", namespace=namespace),
            spec=k8s.ResourceQuotaSpec(
                hard={
                    "requests.cpu": k8s.Quantity.from_string("4"),
                    "requests.memory": k8s.Quantity.from_string("8Gi"),
                    "limits.cpu": k8s.Quantity.from_string("18"),
                    "limits.memory": k8s.Quantity.from_string("28Gi"),
                    "requests.storage": k8s.Quantity.from_string("80Gi"),
                }
            ),
        )
        k8s.KubeLimitRange(
            self,
            "limitrange",
            metadata=k8s.ObjectMeta(name="limits", namespace=namespace),
            spec=k8s.LimitRangeSpec(
                limits=[
                    k8s.LimitRangeItem(
                        type="Container",
                        max={"cpu": k8s.Quantity.from_string("2"), "memory": k8s.Quantity.from_string("4Gi")},
                        min={"cpu": k8s.Quantity.from_string("10m"), "memory": k8s.Quantity.from_string("16Mi")},
                        default={"cpu": k8s.Quantity.from_string("500m"), "memory": k8s.Quantity.from_string("512Mi")},
                        default_request={
                            "cpu": k8s.Quantity.from_string("100m"),
                            "memory": k8s.Quantity.from_string("128Mi"),
                        },
                    ),
                    k8s.LimitRangeItem(
                        type="Pod",
                        max={"cpu": k8s.Quantity.from_string("4"), "memory": k8s.Quantity.from_string("8Gi")},
                    ),
                ]
            ),
        )
