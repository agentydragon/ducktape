"""The `stateful-infra` PriorityClass, and the one priority both its object and the
descheduler's eviction threshold (descheduler.py) are rendered from.

The descheduler's `priorityThreshold` is a hard filter, not a preference: priority only
breaks ties among pods already eligible, and everything below the threshold is eligible.
The class exempts its pods from eviction only because the threshold is this same value.
"""

from __future__ import annotations

from pathlib import Path

from cdk8s import App, Chart
from cdk8s_plus_34 import k8s
from constructs import Construct

from cluster.cdk8s.generation import write_charts
from cluster.cdk8s.manifest_roots import HAND_WRITTEN_ROOT

NAME = "stateful-infra"
# Above ordinary workloads (default 0) so scheduler preemption defers these pods; far below
# system-cluster-critical (2_000_000_000) so they are not treated as control-plane components.
PRIORITY = 1_000_000


def priority_class(scope: Construct) -> k8s.KubePriorityClass:
    """Not `globalDefault`: only pods that opt in via `priorityClassName` get it. Carried by
    the SeaweedFS master/volume/filer/s3 components (cluster/cdk8s/seaweedfs/cluster.py);
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


def priority_class_chart(app: App) -> Chart:
    chart = Chart(app, "priorityclass", disable_resource_name_hashes=True)
    priority_class(chart)
    return chart


def write_seaweedfs_manifests(root: Path) -> None:
    write_charts(root, f"{HAND_WRITTEN_ROOT}/seaweedfs/cluster", priority_class_chart)
