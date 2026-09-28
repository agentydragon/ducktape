"""The parked augur-evidence unit's copies of its git credentials, rendered beside its
hand-written Terraform CR in cluster/k8s/parked/augur-evidence.

tf/gitops/augur-evidence writes both Secrets into the forgejo namespace. They are copied
into budget, where Reflector mirrors them into gaffer-private's augur namespace, as
budget-ledger's are (forgejo/budget_namespace.py). Nothing applies this directory while it
is parked; reviving it applies the copies with the Terraform CR.
"""

from __future__ import annotations

from pathlib import Path

from cdk8s import ApiObjectMetadata, App, Chart
from cdk8s_plus_34 import ServiceAccount

from cluster.cdk8s.forgejo import secret_copy
from cluster.cdk8s.generation import write_charts
from cluster.cdk8s.manifest_roots import HAND_WRITTEN_ROOT

OUTPUT_DIR = f"{HAND_WRITTEN_ROOT}/parked/augur-evidence"


def chart(app: App) -> Chart:
    chart = Chart(app, "augur-evidence", disable_resource_name_hashes=True)
    # Not secret_copy.reader: budget-namespace already owns budget's forgejo-secret-reader,
    # and a revived unit applying the same ServiceAccount would contest its ownership.
    reader = ServiceAccount(
        chart, "reader", metadata=ApiObjectMetadata(name="augur-evidence-secret-reader", namespace="budget")
    )
    for name in ("augur-evidence-git-write", "augur-evidence-git-read"):
        secret_copy.secret_copy(chart, name, reader=reader, mirror_namespaces=["augur"])
    return chart


def write_manifests(root: Path) -> None:
    write_charts(root, OUTPUT_DIR, chart)
