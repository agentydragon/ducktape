"""The `stateful-infra` PriorityClass, and the one priority both its object and the
descheduler's eviction threshold (descheduler_constructs.py) are rendered from.

The descheduler's `priorityThreshold` is a hard filter, not a preference: priority only
breaks ties among pods already eligible, and everything below the threshold is eligible.
The class exempts its pods from eviction only because the threshold is this same value.
"""

from __future__ import annotations

from cdk8s_plus_34 import k8s
from constructs import Construct

NAME = "stateful-infra"
# Above ordinary workloads (default 0) so scheduler preemption defers these pods; far below
# system-cluster-critical (2_000_000_000) so they are not treated as control-plane components.
PRIORITY = 1_000_000


def priority_class(scope: Construct) -> k8s.KubePriorityClass:
    """Not `globalDefault`: only pods that opt in via `priorityClassName` get it. Carried by
    the SeaweedFS master/volume/filer/s3 components (cluster/k8s/seaweedfs/cluster/seaweed.yaml);
    reusable for other stateful infra, see
    cluster/docs/lessons_learned/2026_06_19_seaweedfs_descheduler_dns_race_crashloop.md.
    """
    return k8s.KubePriorityClass(
        scope,
        NAME,
        metadata=k8s.ObjectMeta(
            name=NAME,
            annotations={
                "description": (
                    "Stateful infrastructure (databases, object storage, quorum members). "
                    "Deprioritizes these pods for descheduler eviction and scheduler "
                    "preemption without making them system-critical."
                )
            },
        ),
        value=PRIORITY,
        global_default=False,
        preemption_policy="PreemptLowerPriority",
        description="Stateful infrastructure: defer eviction/preemption, not system-critical.",
    )
