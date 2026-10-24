"""The CNPG Cluster holding Home Assistant's recorder history, its barman-cloud backups, and the
one-off Job importing the SQLite history into it.

One instance beside Home Assistant on the OptiPlex (Optiplex-single, cluster/docs/cnpg_conventions.md
R2), so recorder commits stay on-node. Base backups and WAL go to the `home-assistant-db-backups`
SeaweedFS bucket on OVH, which is what a lost OptiPlex disk recovers from.

Hand-written beside the generated output: `recorder-import/`, the import Job's scripts, which the
`kustomization.yaml`'s configMapGenerator renders into `_IMPORT_SCRIPTS`.
"""

from __future__ import annotations

from pathlib import Path

from cdk8s import App, Chart
from cdk8s_plus_34 import k8s

from cluster.cdk8s import cnpg, cnpg_backup, node_scheduling
from cluster.cdk8s.generation import write_charts

# Renamed: `app` is the cdk8s `App` parameter of `chart`.
from cluster.cdk8s.home_assistant import app as home_assistant
from cluster.cdk8s.manifest_roots import HAND_WRITTEN_ROOT

_OUTPUT_DIR = f"{HAND_WRITTEN_ROOT}/home-assistant"
# Home Assistant's recorder will read `uri` from this Cluster's generated `-app` Secret.
DATABASE = cnpg.PostgresRef.generated(name="home-assistant-db", namespace="home-assistant")
# CLEANUP(added 2026-10-24): Remove the import Job, its scripts and this ConfigMap once the
#   recorder runs on DATABASE (the change setting `recorder: db_url` drops them).
_IMPORT_SCRIPTS = "recorder-import"


def _import_job(chart: Chart) -> None:
    db_url = DATABASE.app_secret.key("uri").env_var("RECORDER_DB_URL")
    work = k8s.VolumeMount(name="work", mount_path="/work")
    scripts = k8s.VolumeMount(name="scripts", mount_path="/scripts", read_only=True)
    resources = k8s.ResourceRequirements(
        requests={"cpu": k8s.Quantity.from_string("250m"), "memory": k8s.Quantity.from_string("512Mi")},
        limits={"memory": k8s.Quantity.from_string("2Gi")},
    )
    k8s.KubeJob(
        chart,
        "recorder-import",
        metadata=k8s.ObjectMeta(
            name="recorder-import",
            namespace=DATABASE.namespace,
            # Flux replaces this Job whenever its template changes.
            annotations={"kustomize.toolkit.fluxcd.io/force": "enabled"},
        ),
        spec=k8s.JobSpec(
            backoff_limit=0,
            template=k8s.PodTemplateSpec(
                spec=k8s.PodSpec(
                    restart_policy="Never",
                    automount_service_account_token=False,
                    security_context=k8s.PodSecurityContext(seccomp_profile=k8s.SeccompProfile(type="RuntimeDefault")),
                    # The config PVC is node-local to the OptiPlex.
                    node_selector=node_scheduling.OPTIPLEX.node_selector,
                    init_containers=[
                        k8s.Container(
                            name="prepare",
                            # The live Deployment's version: it wrote the SQLite file, and the
                            # load copies its rows into the schema this version creates.
                            image=home_assistant.IMAGE,
                            command=["python3", "/scripts/prepare.py"],
                            env=[db_url],
                            resources=resources,
                            # Read-write although only read: opening a WAL-mode database needs its `-shm`.
                            volume_mounts=[k8s.VolumeMount(name="config", mount_path="/config"), work, scripts],
                        )
                    ],
                    containers=[
                        k8s.Container(
                            name="load",
                            image="ghcr.io/dimitri/pgloader:3.6.9",
                            command=["sh", "/scripts/load.sh"],
                            env=[db_url],
                            resources=resources,
                            volume_mounts=[work, scripts],
                        )
                    ],
                    volumes=[
                        k8s.Volume(
                            name="config",
                            persistent_volume_claim=k8s.PersistentVolumeClaimVolumeSource(
                                claim_name=home_assistant.CONFIG_CLAIM
                            ),
                        ),
                        k8s.Volume(name="work", empty_dir=k8s.EmptyDirVolumeSource()),
                        k8s.Volume(name="scripts", config_map=k8s.ConfigMapVolumeSource(name=_IMPORT_SCRIPTS)),
                    ],
                )
            ),
        ),
    )


def chart(app: App) -> Chart:
    chart = Chart(app, DATABASE.name, disable_resource_name_hashes=True)
    cnpg.cluster(
        chart,
        "cluster",
        ref=DATABASE,
        profile=cnpg.OPTIPLEX_PIRAEUS_SSD,
        instances=1,
        # A year of GPS fixes plus 30 days of everything else.
        size="10Gi",
        initdb=cnpg.same_owner_initdb("homeassistant"),
        wal_archive=True,
    )
    cnpg_backup.CnpgBackup(chart, "db-backup", ref=DATABASE, retention="30d")
    _import_job(chart)
    return chart


def write_manifests(root: Path) -> None:
    write_charts(root, _OUTPUT_DIR, chart, manifest_name="db.k8s.yaml")
