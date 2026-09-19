"""The change-driven release gate: this exact console image migrates the schema before Flux
rolls the API/static workloads that depend on it. A migration failure is a release failure,
not a transient workload retry: one attempt, its pod and logs preserved until an operator
has diagnosed or deliberately retried it (cluster/docs/troubleshooting.md).

The Job stays image-coupled with the API Deployment through the same image-pins/ entry. A
versioned Job name would instead bind rollout state to repository HEAD and duplicate
alembic_version.
"""

from __future__ import annotations

from cdk8s import ApiObject, ApiObjectMetadata, Duration, JsonPatch, Size
from cdk8s_plus_34 import (
    ContainerResources,
    Cpu,
    CpuResources,
    ImagePullPolicy,
    Job,
    MemoryResources,
    PodSecurityContextProps,
    RestartPolicy,
    ServiceAccount,
)
from constructs import Construct

from cluster.cdk8s.agentplane import container_security, node_scheduling
from cluster.cdk8s.forgejo_images import forgejo_images_creds_secret_ref
from cluster.cdk8s.haku import console_constructs
from cluster.cdk8s.metadata import metadata
from cluster.cdk8s.pod_spec_patches import runtime_default_seccomp_patch

NAME = "haku-console-migration"


class Migration(Construct):
    """The migration ServiceAccount and Job."""

    def __init__(self, scope: Construct, id: str) -> None:
        super().__init__(scope, id)
        namespace = console_constructs.NAMESPACE
        # No Kubernetes API work: separate from the API ServiceAccount, which can manage
        # narrowly scoped sandbox claims.
        service_account = ServiceAccount(
            self, "serviceaccount", metadata=metadata(NAME, namespace), automount_token=False
        )
        job = Job(
            self,
            "job",
            metadata=metadata(NAME, namespace, annotations={"kustomize.toolkit.fluxcd.io/force": "enabled"}),
            pod_metadata=ApiObjectMetadata(labels={"app.kubernetes.io/name": NAME}),
            select=False,
            backoff_limit=0,
            active_deadline=Duration.seconds(300),
            restart_policy=RestartPolicy.NEVER,
            service_account=service_account,
            automount_service_account_token=False,
            enable_service_links=False,
            docker_registry_auth=forgejo_images_creds_secret_ref(self, "forgejo-images-creds-ref"),
            security_context=PodSecurityContextProps(ensure_non_root=True, user=1000, group=1000),
        )
        job.add_container(
            name="migrate",
            image=f"{console_constructs.IMAGE}:{console_constructs.PLACEHOLDER_TAG}",
            image_pull_policy=ImagePullPolicy.ALWAYS,
            args=["migrate"],
            # The migration command consumes only the database URL.
            env_variables=console_constructs.database_env(self),
            resources=ContainerResources(
                cpu=CpuResources(request=Cpu.millis(50)),
                memory=MemoryResources(request=Size.mebibytes(128), limit=Size.mebibytes(512)),
            ),
            security_context=container_security.WRITABLE_ROOT,
        )
        node_scheduling.attract_to_zone(job)
        ApiObject.of(job).add_json_patch(runtime_default_seccomp_patch())
        ApiObject.of(job).add_json_patch(
            JsonPatch.add("/spec/template/spec/containers/0/terminationMessagePolicy", "FallbackToLogsOnError")
        )
