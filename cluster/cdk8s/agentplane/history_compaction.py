"""The one-off Job that settles history stored before delta settlement
(agentplane/history_service/README.md#delta-settlement).

A dry run only logs what settling would remove. An apply run first dumps the three history tables
to a volume kept for rollback, then settles. The Job keeps its name per run kind and has no TTL, so
a finished run stays put instead of Flux recreating it; switching the environment's run kind or
pinning a new image replaces it, which reruns it. Both runs are safe to repeat: settling resumes
where it stopped, and the dump is taken once.
"""

from cdk8s import ApiObjectMetadata, Duration, Size
from cdk8s_plus_34 import (
    ContainerProps,
    ContainerResources,
    ContainerSecurityContextProps,
    Cpu,
    CpuResources,
    EnvValue,
    ImagePullPolicy,
    Job,
    MemoryResources,
    PersistentVolumeAccessMode,
    PersistentVolumeClaim,
    PodSecurityContextProps,
    RestartPolicy,
    Volume,
    VolumeMount,
)
from constructs import Construct

from agentplane.history_service.compaction_settings import CompactionSettings
from cluster.cdk8s import cilium, cnpg, node_scheduling, pod_policy
from cluster.cdk8s.agentplane import database
from cluster.cdk8s.agentplane.environment import CompactionRun, Environment
from cluster.cdk8s.forgejo_registry.chart import forgejo_images_creds_secret_ref
from cluster.cdk8s.providers.cilium.network_policy import NetworkPolicy
from cluster.cdk8s.secret_ref import SecretRef
from util.settings_contract import env_name

NAME = "agentplane-history-compact"
_IMAGE = "git.allegedly.works/ducktape-ci/agentplane-history-compact"
_LABELS = {"app.kubernetes.io/name": NAME}
_DUMP_DIR = "/dump"
_DUMP = f"{_DUMP_DIR}/history.dump"


class HistoryCompaction(Construct):
    def __init__(self, scope: Construct, id: str, env: Environment, run: CompactionRun) -> None:
        super().__init__(scope, id)
        database_url = (
            SecretRef(namespace=env.namespace, name="postgres-sandbox-service").key("uri").env_value(self, "database")
        )
        init_containers: list[ContainerProps] = []
        volumes: list[Volume] = []
        if run is CompactionRun.APPLY:
            dump = Volume.from_persistent_volume_claim(
                self,
                "dump-volume",
                PersistentVolumeClaim(
                    self,
                    "dump",
                    metadata=ApiObjectMetadata(
                        name=f"{NAME}-dump",
                        namespace=env.namespace,
                        annotations={
                            "description": (
                                "pg_dump of session_event and its settlement tables taken before the "
                                "compaction apply; kept for rollback, deleted with the apply run."
                            )
                        },
                    ),
                    storage=Size.gibibytes(10),
                    storage_class_name="seaweedfs-ovh",
                    access_modes=[PersistentVolumeAccessMode.READ_WRITE_ONCE],
                ),
            )
            volumes.append(dump)
            init_containers.append(
                ContainerProps(
                    name="dump",
                    image=cnpg.POSTGRES_IMAGE,
                    image_pull_policy=ImagePullPolicy.IF_NOT_PRESENT,
                    # A retried Pod keeps the dump taken before its first apply attempt.
                    command=[
                        "sh",
                        "-c",
                        f"test -e {_DUMP} || {{ pg_dump -Fc -t session_event -t session_event_settlement "
                        f'-t session_event_settled_range -f {_DUMP}.partial "$DATABASE_URL" '
                        f"&& mv {_DUMP}.partial {_DUMP}; }}",
                    ],
                    env_variables={"DATABASE_URL": database_url},
                    volume_mounts=[VolumeMount(path=_DUMP_DIR, volume=dump)],
                    resources=ContainerResources(
                        cpu=CpuResources(request=Cpu.millis(100)),
                        memory=MemoryResources(request=Size.mebibytes(128), limit=Size.mebibytes(512)),
                    ),
                    security_context=ContainerSecurityContextProps(read_only_root_filesystem=False),
                )
            )
        job = Job(
            self,
            "job",
            metadata=ApiObjectMetadata(
                name=f"{NAME}-{run}",
                namespace=env.namespace,
                annotations={"kustomize.toolkit.fluxcd.io/force": "enabled"},
            ),
            pod_metadata=ApiObjectMetadata(labels=_LABELS),
            select=False,
            # A run resumes where the last one stopped, so a retry repeats no work.
            backoff_limit=3,
            active_deadline=Duration.hours(6),
            restart_policy=RestartPolicy.NEVER,
            automount_service_account_token=False,
            enable_service_links=False,
            docker_registry_auth=forgejo_images_creds_secret_ref(self, "images-creds"),
            security_context=PodSecurityContextProps(ensure_non_root=True, user=1000, group=1000, fs_group=1000),
            init_containers=init_containers,
            volumes=volumes,
        )
        env_variables = {env_name(CompactionSettings, "database_url"): database_url}
        if run is CompactionRun.APPLY:
            env_variables[env_name(CompactionSettings, "apply")] = EnvValue.from_value("true")
        job.add_container(
            name="compact",
            image=f"{_IMAGE}:unset",
            image_pull_policy=ImagePullPolicy.IF_NOT_PRESENT,
            env_variables=env_variables,
            resources=ContainerResources(
                cpu=CpuResources(request=Cpu.millis(100)),
                memory=MemoryResources(request=Size.mebibytes(256), limit=Size.mebibytes(1024)),
            ),
            security_context=ContainerSecurityContextProps(read_only_root_filesystem=False),
        )
        pod_policy.place(job, node_scheduling.HIL_OVH)
        pod_policy.harden(job)
        NetworkPolicy(
            self,
            "network-policy",
            metadata=ApiObjectMetadata(name=NAME, namespace=env.namespace),
            endpoint_selector=_LABELS,
            egress=[cilium.dns_egress(), database.egress(env.namespace)],
        )
