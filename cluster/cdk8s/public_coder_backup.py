"""The public-coder-agent state backup: a `ResticBackup` into its own SeaweedFS bucket. The
SOPS-encrypted Restic password beside the output stays hand-written.
"""

from __future__ import annotations

from cdk8s import App, Chart
from volsync_replicationsource_crds.backube.volsync import (
    ReplicationSourceSpecResticMoverAffinity,
    ReplicationSourceSpecResticMoverAffinityNodeAffinity,
    ReplicationSourceSpecResticMoverAffinityNodeAffinityRequiredDuringSchedulingIgnoredDuringExecution,
    ReplicationSourceSpecResticMoverAffinityNodeAffinityRequiredDuringSchedulingIgnoredDuringExecutionNodeSelectorTerms,
    ReplicationSourceSpecResticMoverAffinityNodeAffinityRequiredDuringSchedulingIgnoredDuringExecutionNodeSelectorTermsMatchExpressions,
)

from cluster.cdk8s.flux import Kustomization, RenderedDirectory, flux_kustomization, flux_kustomization_depends_on_many
from cluster.cdk8s.manifest_roots import HAND_WRITTEN_ROOT
from cluster.cdk8s.restic_backup import ResticBackup
from cluster.cdk8s.seaweedfs import s3

NAME = "public-coder-agent-backup"
OUTPUT_DIR = f"{HAND_WRITTEN_ROOT}/agents/public-coder-agent/backup"
_NAMESPACE = "public-coder-agent"
_BUCKET_NAME = "public-coder-agent-backups"
_S3_CREDENTIALS_SECRET_NAME = "public-coder-agent-seaweedfs-credentials"


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    s3.PrivateBucket(
        chart,
        "storage",
        name=_BUCKET_NAME,
        tenant=_NAMESPACE,
        adopt_existing=True,
        description="Public Coder's tenant-local SeaweedFS backup bucket.",
        secret_name=_S3_CREDENTIALS_SECRET_NAME,
    )
    # The worker-local OpenClaw state. Run and retain a restore drill before treating these
    # snapshots as a replacement for the old PVC/rescue archive.
    ResticBackup(
        chart,
        "backup",
        namespace=_NAMESPACE,
        name="public-coder-agent-state-v2-restic",
        repository="public-coder-agent-state-v2-restic",
        source_pvc="public-coder-agent-state-v2",
        bucket=_BUCKET_NAME,
        s3_credentials=_S3_CREDENTIALS_SECRET_NAME,
        # 09:17 UTC is 02:17 PDT / 01:17 PST; run away from normal interactive use.
        schedule="17 09 * * *",
        cache_storage_class_name="local-path-ovh-hdd",
        mover_affinity=ReplicationSourceSpecResticMoverAffinity(
            node_affinity=ReplicationSourceSpecResticMoverAffinityNodeAffinity(
                required_during_scheduling_ignored_during_execution=ReplicationSourceSpecResticMoverAffinityNodeAffinityRequiredDuringSchedulingIgnoredDuringExecution(
                    node_selector_terms=[
                        ReplicationSourceSpecResticMoverAffinityNodeAffinityRequiredDuringSchedulingIgnoredDuringExecutionNodeSelectorTerms(
                            match_expressions=[
                                ReplicationSourceSpecResticMoverAffinityNodeAffinityRequiredDuringSchedulingIgnoredDuringExecutionNodeSelectorTermsMatchExpressions(
                                    key="kubernetes.io/hostname", operator="In", values=["ovh-ns102453"]
                                )
                            ]
                        )
                    ]
                )
            )
        ),
    )
    return chart


def public_coder_agent_backup(
    chart: Chart,
    directory: RenderedDirectory,
    seaweedfs_operator: Kustomization,
    external_secrets_operator: Kustomization,
    volsync: Kustomization,
) -> Kustomization:
    return flux_kustomization(
        chart,
        NAME,
        directory,
        timeout="5m",
        depends_on=flux_kustomization_depends_on_many(seaweedfs_operator, external_secrets_operator, volsync),
        description=(
            "Restic/VolSync backup of Public Coder's worker-local OpenClaw state "
            "to its dedicated private SeaweedFS S3 bucket."
        ),
    )
