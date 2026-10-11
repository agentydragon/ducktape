"""The one-off Job that settles history stored before delta settlement
(agentplane/history_service/README.md#delta-settlement).

A dry run only logs what settling would remove; an apply run settles. Take a CNPG backup before
the first apply (agentplane/db_backups.py): the Job keeps no copy of what it removes. The Job keeps
its name per run kind and has no TTL, so a finished run stays put instead of Flux recreating it;
switching the environment's run kind or pinning a new image replaces it, which reruns it. Both runs
are safe to repeat: settling resumes where it stopped.
"""

from cdk8s import ApiObjectMetadata, Duration, Size
from cdk8s_plus_34 import (
    ContainerResources,
    ContainerSecurityContextProps,
    Cpu,
    CpuResources,
    EnvValue,
    ImagePullPolicy,
    Job,
    MemoryResources,
    PodSecurityContextProps,
    RestartPolicy,
)
from constructs import Construct

from agentplane.history_service.compaction_settings import CompactionSettings
from cluster.cdk8s import cilium, node_scheduling, pod_policy
from cluster.cdk8s.agentplane import database
from cluster.cdk8s.agentplane.environment import CompactionRun, Environment
from cluster.cdk8s.forgejo_registry.chart import forgejo_images_creds_secret_ref
from cluster.cdk8s.providers.cilium.network_policy import NetworkPolicy
from cluster.cdk8s.secret_ref import SecretRef
from util.settings_contract import env_name

NAME = "agentplane-history-compact"
_IMAGE = "git.allegedly.works/ducktape-ci/agentplane-history-compact"
_LABELS = {"app.kubernetes.io/name": NAME}


class HistoryCompaction(Construct):
    def __init__(self, scope: Construct, id: str, env: Environment, run: CompactionRun) -> None:
        super().__init__(scope, id)
        database_url = (
            SecretRef(namespace=env.namespace, name="postgres-sandbox-service").key("uri").env_value(self, "database")
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
            egress=[
                cilium.dns_egress(),
                database.egress(env.namespace),
            ],
        )
