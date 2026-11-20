"""Ducktape's tofu-controller `Terraform` CRs, built on `providers/tofu_controller`. Each one runs
as `tf-runner` in flux-system and keeps its state in the OVH tofu-state Postgres. `gitops_terraform`
builds the one for each `tf/gitops/<name>` module, reading the module's own artifact
(`gitops_module`).

tofu-controller replans whenever its source's artifact revision moves. The `ducktape`
GitRepository's revision is the `devel` commit, so a CR reading it would replan on every
commit to the repository; an ArtifactGenerator artifact's revision is the digest of what it
packages, so a CR reading its module's artifact replans only when that module changes, its own
spec changes, or its interval fires."""

from __future__ import annotations

import posixpath
from collections.abc import Mapping, Sequence

from cdk8s import ApiObjectMetadata
from constructs import Construct
from pydantic import BaseModel
from source_watcher_crds.io.fluxcd.extensions.source import ArtifactGeneratorSpecArtifacts
from tofu_controller.io.fluxcd.contrib.infra import (
    Terraform,
    TerraformSpec,
    TerraformSpecBackendConfig,
    TerraformSpecRunnerPodTemplate,
    TerraformSpecRunnerPodTemplateSpec,
    TerraformSpecRunnerPodTemplateSpecEnv,
    TerraformSpecRunnerPodTemplateSpecEnvValueFrom,
    TerraformSpecRunnerPodTemplateSpecEnvValueFromSecretKeyRef,
    TerraformSpecRunnerPodTemplateSpecResources,
    TerraformSpecRunnerPodTemplateSpecResourcesRequests,
    TerraformSpecRunnerPodTemplateSpecVolumeMounts,
    TerraformSpecRunnerPodTemplateSpecVolumes,
    TerraformSpecSourceRef,
    TerraformSpecSourceRefKind,
    TerraformSpecStoreReadablePlan,
    TerraformSpecVars,
)

from cluster.cdk8s import ducktape_flux, flux
from cluster.cdk8s.artifact_generators import artifact
from cluster.cdk8s.flux import artifact_directory
from cluster.cdk8s.secret_ref import SecretKey
from cluster.cdk8s.tofu_state import db

NAMESPACE = "flux-system"

# Requests only. Without them runner pods are BestEffort, so the scheduler counts them as free and
# stacks a synchronised wave of up to a dozen on one node. Observed per runner: median working set
# 90 MiB, peak 516 MiB (dns-records, the AWS provider), 20-35 CPU-seconds per run. A memory limit
# would OOM-kill a plan mid-run.
_RUNNER_RESOURCES = TerraformSpecRunnerPodTemplateSpecResources(
    requests={
        "cpu": TerraformSpecRunnerPodTemplateSpecResourcesRequests.from_string("250m"),
        "memory": TerraformSpecRunnerPodTemplateSpecResourcesRequests.from_string("512Mi"),
    }
)


def gitops_module(name: str) -> ArtifactGeneratorSpecArtifacts:
    """The artifact packaging the `tf/gitops/<name>` module, which its `gitops_terraform` CR reads."""
    return artifact(f"tf-gitops-{name}", f"{ducktape_flux.TF_GITOPS_ROOT}/{name}")


def gitops_state_schema(name: str) -> str:
    """The default Postgres backend schema for `tf/gitops/<name>`."""
    return name.replace("-", "_")


def secret_env(name: str, key: SecretKey) -> TerraformSpecRunnerPodTemplateSpecEnv:
    """The tofu-controller CRD's own env struct: its schema, not `k8s.EnvVar`."""
    return TerraformSpecRunnerPodTemplateSpecEnv(
        name=name,
        value_from=TerraformSpecRunnerPodTemplateSpecEnvValueFrom(
            secret_key_ref=TerraformSpecRunnerPodTemplateSpecEnvValueFromSecretKeyRef(name=key.secret.name, key=key.key)
        ),
    )


def tofu_state_terraform(
    scope: Construct,
    id: str,
    *,
    name: str,
    schema: str,
    source_ref: TerraformSpecSourceRef,
    path: str,
    interval: str,
    annotations: Mapping[str, str] | None = None,
    approve_plan: str | None = None,
    plan_only: bool | None = None,
    store_readable_plan: TerraformSpecStoreReadablePlan | None = None,
    targets: Sequence[str] | None = None,
    vars: Sequence[TerraformSpecVars] | None = None,
    env: Sequence[TerraformSpecRunnerPodTemplateSpecEnv] = (),
    node_selector: Mapping[str, str] | None = None,
    volumes: Sequence[TerraformSpecRunnerPodTemplateSpecVolumes] = (),
    volume_mounts: Sequence[TerraformSpecRunnerPodTemplateSpecVolumeMounts] = (),
) -> Terraform:
    """A `Terraform` in flux-system, run by `tf-runner`, with its state under `schema` in the
    tofu-state Postgres; the runner reads that database's password as `PGPASSWORD`, ahead of `env`.
    Every plan refreshes first: without it a plan trusts the state and misses reality drifting from
    it (cluster/docs/lessons_learned/2026_04_25_tofu_controller_refresh_before_apply.md). The other
    keywords are `TerraformSpec` fields under their own names.
    """
    return Terraform(
        scope,
        id,
        metadata=ApiObjectMetadata(name=name, namespace=NAMESPACE, annotations=annotations),
        spec=TerraformSpec(
            interval=interval,
            source_ref=source_ref,
            path=path,
            approve_plan=approve_plan,
            plan_only=plan_only,
            refresh_before_apply=True,
            store_readable_plan=store_readable_plan,
            targets=targets,
            service_account_name="tf-runner",
            backend_config=TerraformSpecBackendConfig(
                custom_configuration=f'backend "pg" {{\n  conn_str    = "{db.STATE_CONNECTION_STRING}"\n  schema_name = "{schema}"\n}}\n'
            ),
            vars=vars,
            runner_pod_template=TerraformSpecRunnerPodTemplate(
                spec=TerraformSpecRunnerPodTemplateSpec(
                    env=[secret_env("PGPASSWORD", db.STATE_PASSWORD), *env],
                    node_selector=node_selector,
                    resources=_RUNNER_RESOURCES,
                    volumes=list(volumes) or None,
                    volume_mounts=list(volume_mounts) or None,
                )
            ),
        ),
    )


def gitops_terraform(
    scope: Construct,
    id: str,
    *,
    module: ArtifactGeneratorSpecArtifacts,
    variables: BaseModel | None,
    store_readable_plan: TerraformSpecStoreReadablePlan | None = None,
    auto_apply: bool = True,
    node_selector: Mapping[str, str] | None = None,
    volumes: Sequence[TerraformSpecRunnerPodTemplateSpecVolumes] = (),
    volume_mounts: Sequence[TerraformSpecRunnerPodTemplateSpecVolumeMounts] = (),
) -> Terraform:
    """The module `module` packages (`gitops_module`), run from that artifact and, unless
    `auto_apply=False`, auto-approved. Without auto-apply each run plans and waits for a person to
    set `approvePlan` to that plan's id, so drift is reported but nothing changes unasked.
    The module directory's name is the CR's name and, underscored, its state schema.

    `variables` models the module's variables.tf (None: set none); each field is written
    structurally into the runner's tfvars, so a nested map arrives as a Terraform
    map/object, not a string.

    `store_readable_plan=HUMAN` writes each plan's diff to the `tfplan-default-<name>`
    ConfigMap, readable by anyone who can read ConfigMaps in flux-system. Enable it only
    for modules whose providers mark every secret-bearing attribute `Sensitive`, which
    the plan masks.

    `node_selector` pins the runner pod, for a module whose provider reaches something only
    some nodes can (a device on the home LAN). `volumes` and `volume_mounts` add files to the
    runner pod, such as a CA bundle a provider verifies against.
    """
    directory = artifact_directory(module)
    name = posixpath.basename(directory)
    return tofu_state_terraform(
        scope,
        id,
        name=name,
        schema=gitops_state_schema(name),
        source_ref=TerraformSpecSourceRef(
            kind=TerraformSpecSourceRefKind.EXTERNAL_ARTIFACT, name=module.name, namespace=flux.NAMESPACE
        ),
        path=f"./{directory}",
        interval="15m",
        approve_plan="auto" if auto_apply else None,
        store_readable_plan=store_readable_plan,
        vars=[
            TerraformSpecVars(name=key, value=value)
            for key, value in (variables.model_dump(mode="json").items() if variables is not None else ())
        ]
        or None,
        node_selector=node_selector,
        volumes=volumes,
        volume_mounts=volume_mounts,
    )
