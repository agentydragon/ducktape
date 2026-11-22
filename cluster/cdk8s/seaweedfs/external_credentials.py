"""The `seaweedfs-credentials` namespace holding the canonical, externally managed SeaweedFS
S3 credential Secrets (hand-written `*.sops.yaml` beside the output), and the grants that
let only `S3Credentials` in the operator's namespace read each one.

The Secrets live outside the operator's namespace so the operator consumes them read-only;
`public_s3` registers them with native IAM.
"""

from __future__ import annotations

from cdk8s import App, Chart

from cluster.cdk8s import namespaces
from cluster.cdk8s.manifest_roots import HAND_WRITTEN_ROOT
from cluster.cdk8s.namespaces import Vpa
from cluster.cdk8s.seaweedfs import s3

NAMESPACE = "seaweedfs-credentials"
OUTPUT_DIR = f"{HAND_WRITTEN_ROOT}/seaweedfs/external-credentials"
CLAUDE_READER_SECRET = "claude-reader-s3-credentials"
DRIVEFS_ARTIFACTS_SECRET = "drivefs-artifacts-s3-credentials"
_CHART = "external-credentials"


def chart(app: App) -> Chart:
    chart = Chart(app, _CHART, disable_resource_name_hashes=True)
    namespaces.namespace(
        chart,
        "namespace",
        name=NAMESPACE,
        vpa=Vpa.RECOMMEND,
        labels={"name": NAMESPACE},
        annotations={"description": "Externally managed SeaweedFS S3 credential source Secrets."},
    )
    for secret in (CLAUDE_READER_SECRET, DRIVEFS_ARTIFACTS_SECRET):
        s3.secret_grant(chart, secret=secret, namespace=NAMESPACE)
    return chart
