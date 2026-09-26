"""The change-driven schema migration: this exact console image migrates before the API
serves the new schema.

It shares one Flux Kustomization with the database and the console, which gives it no
ordering guarantee against either -- so it retries instead, until CNPG has bootstrapped
and the Cluster accepts connections. Every attempt leaves its own Pod behind (a Job with
`restartPolicy: Never` creates one per try and does not delete failures), so a genuine
migration bug is still diagnosable from the logs; what it no longer gets is a single
attempt. The Kustomization's `wait` plus this Job's health check keep dependent
Kustomizations from reconciling until it succeeds.

The Job stays image-coupled with the API Deployment through the same image-pins/ entry. A
versioned Job name would instead bind rollout state to repository HEAD and duplicate
alembic_version.
"""

from __future__ import annotations

from cdk8s import ApiObject, Duration, JsonPatch, Size
from cdk8s_plus_34 import (
    ContainerResources,
    Cpu,
    CpuResources,
    ImagePullPolicy,
    Job,
    MemoryResources,
    RestartPolicy,
    ServiceAccount,
)
from constructs import Construct

from cluster.cdk8s.agentplane import container_security, node_scheduling
from cluster.cdk8s.forgejo_images import forgejo_images_creds_secret_ref
from cluster.cdk8s.haku import console
from cluster.cdk8s.metadata import metadata
from cluster.cdk8s.pod_hardening import hardened_pod_defaults
from cluster.cdk8s.pod_spec_patches import runtime_default_seccomp_patch

NAME = "haku-console-migration"


class Migration(Construct):
    """The migration ServiceAccount and Job."""

    def __init__(self, scope: Construct, id: str) -> None:
        super().__init__(scope, id)
        namespace = console.NAMESPACE
        # No Kubernetes API work: separate from the API ServiceAccount, which can manage
        # narrowly scoped sandbox claims.
        service_account = ServiceAccount(
            self, "serviceaccount", metadata=metadata(NAME, namespace), automount_token=False
        )
        job = Job(
            self,
            "job",
            metadata=metadata(NAME, namespace, annotations={"kustomize.toolkit.fluxcd.io/force": "enabled"}),
            select=False,
            # Retries are how this waits for the database, since nothing sequences the two
            # inside one Kustomization. Kubernetes backs off exponentially to a 6m ceiling,
            # so ~10 attempts spans well over the deadline; the deadline is the real bound
            # and covers every attempt, not each one.
            backoff_limit=10,
            active_deadline=Duration.minutes(20),
            restart_policy=RestartPolicy.NEVER,
            service_account=service_account,
            docker_registry_auth=forgejo_images_creds_secret_ref(self, "forgejo-images-creds-ref"),
            **hardened_pod_defaults({"app.kubernetes.io/name": NAME}, uid=1000, gid=1000),
        )
        job.add_container(
            name="migrate",
            image=f"{console.IMAGE}:{console.PLACEHOLDER_TAG}",
            image_pull_policy=ImagePullPolicy.ALWAYS,
            args=["migrate"],
            # The migration command consumes only the database URL.
            env_variables=console.database_env(self),
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
