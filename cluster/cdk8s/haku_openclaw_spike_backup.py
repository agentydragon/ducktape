"""The haku-openclaw-spike state backup: a `ResticBackup` into the app's SeaweedFS bucket
(`haku_openclaw_spike_config._backup_bucket`). The SOPS-encrypted Restic password beside the
output stays hand-written.
"""

from __future__ import annotations

from cdk8s import App, Chart

from cluster.cdk8s.flux import Kustomization, RenderedDirectory, flux_kustomization, flux_kustomization_depends_on_many
from cluster.cdk8s.manifest_roots import HAND_WRITTEN_ROOT
from cluster.cdk8s.restic_backup import ResticBackup

NAME = "haku-openclaw-spike-backup"
OUTPUT_DIR = f"{HAND_WRITTEN_ROOT}/agents/haku-openclaw-spike/backup"


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    # The source is the optiplex home worker PVC (local-path-home-ssd). No mover affinity: the
    # Direct mover mounts that already-bound PVC, so its PV's node affinity places the mover and
    # its WaitForFirstConsumer cache on optiplex. A restore into an unbound PVC is the opposite
    # case: there the mover's placement chooses the node, so it must be pinned.
    ResticBackup(
        chart,
        "backup",
        namespace="haku-openclaw-spike",
        name="haku-openclaw-spike-state-restic",
        repository="haku-openclaw-spike-volsync-restic",
        source_pvc="haku-openclaw-spike-state-v2",
        # The app's Bucket, and the Secret its S3Credentials writes.
        bucket="haku-openclaw-spike-backups",
        s3_credentials="haku-openclaw-spike-volsync-s3-credentials",
        # 03:23 UTC, away from interactive use and staggered from public-coder's 09:17.
        schedule="23 03 * * *",
        cache_storage_class_name="local-path-home-ssd",
    )
    return chart


def haku_openclaw_spike_backup(
    chart: Chart, directory: RenderedDirectory, external_secrets_operator: Kustomization, volsync: Kustomization
) -> Kustomization:
    return flux_kustomization(
        chart,
        NAME,
        directory,
        timeout="5m",
        depends_on=flux_kustomization_depends_on_many(
            # Backup/S3 wiring must converge even when the OpenClaw Deployment is down.
            # The Bucket and S3Credentials remain app-owned, but their readiness is
            # retried by the ExternalSecret rather than coupling this Kustomization to
            # the app Deployment health check.
            external_secrets_operator,
            volsync,
        ),
        description=(
            "Restic/VolSync backup of the Haku OpenClaw spike state to its "
            "dedicated private SeaweedFS S3 bucket, plus the one-shot restore "
            "into the optiplex worker PVC that migrates the state off the control "
            "plane."
        ),
    )
