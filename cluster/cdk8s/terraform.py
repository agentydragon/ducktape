"""Ducktape's tofu-controller `Terraform` CRs, built on `providers/tofu_controller`. Each one runs
as `tf-runner` in flux-system and keeps its state in the OVH tofu-state Postgres. `gitops_terraform`
builds the one for each `tf/gitops/<name>` module."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from cdk8s import ApiObjectMetadata
from constructs import Construct
from pydantic import BaseModel
from tofu_controller.io.fluxcd.contrib.infra import (
    TerraformV1Alpha2SpecBackendConfig,
    TerraformV1Alpha2SpecDependsOn,
    TerraformV1Alpha2SpecRunnerPodTemplate,
    TerraformV1Alpha2SpecRunnerPodTemplateSpec,
    TerraformV1Alpha2SpecRunnerPodTemplateSpecEnv,
    TerraformV1Alpha2SpecRunnerPodTemplateSpecEnvFrom,
    TerraformV1Alpha2SpecRunnerPodTemplateSpecEnvFromSecretRef,
    TerraformV1Alpha2SpecRunnerPodTemplateSpecEnvValueFrom,
    TerraformV1Alpha2SpecRunnerPodTemplateSpecEnvValueFromSecretKeyRef,
    TerraformV1Alpha2SpecRunnerPodTemplateSpecResources,
    TerraformV1Alpha2SpecRunnerPodTemplateSpecResourcesRequests,
    TerraformV1Alpha2SpecRunnerPodTemplateSpecVolumeMounts,
    TerraformV1Alpha2SpecRunnerPodTemplateSpecVolumes,
    TerraformV1Alpha2SpecSourceRef,
    TerraformV1Alpha2SpecSourceRefKind,
    TerraformV1Alpha2SpecStoreReadablePlan,
    TerraformV1Alpha2SpecVars,
)

from cluster.cdk8s import ducktape_flux, flux
from cluster.cdk8s.providers.tofu_controller.terraform import Terraform
from cluster.cdk8s.secret_ref import SecretKey, SecretRef
from cluster.cdk8s.tofu_state import db

NAMESPACE = "flux-system"
_STATE_DB = f"postgres://tfstate@{db.DATABASE.rw.host}:{db.DATABASE.rw.port.number}/tfstate?sslmode=disable"
_STATE_DB_PASSWORD = SecretRef(namespace=NAMESPACE, name="tofu-state-db-credentials").key("password")

# Requests only. Without them runner pods are BestEffort, so the scheduler counts them as free and
# stacks a synchronised wave of up to a dozen on one node. Observed per runner: median working set
# 90 MiB, peak 516 MiB (dns-records, the AWS provider), 20-35 CPU-seconds per run. A memory limit
# would OOM-kill a plan mid-run.
_RUNNER_RESOURCES = TerraformV1Alpha2SpecRunnerPodTemplateSpecResources(
    requests={
        "cpu": TerraformV1Alpha2SpecRunnerPodTemplateSpecResourcesRequests.from_string("250m"),
        "memory": TerraformV1Alpha2SpecRunnerPodTemplateSpecResourcesRequests.from_string("512Mi"),
    }
)


def secret_env(name: str, key: SecretKey) -> TerraformV1Alpha2SpecRunnerPodTemplateSpecEnv:
    """The tofu-controller CRD's own env struct: its schema, not `k8s.EnvVar`."""
    return TerraformV1Alpha2SpecRunnerPodTemplateSpecEnv(
        name=name,
        value_from=TerraformV1Alpha2SpecRunnerPodTemplateSpecEnvValueFrom(
            secret_key_ref=TerraformV1Alpha2SpecRunnerPodTemplateSpecEnvValueFromSecretKeyRef(
                name=key.secret.name, key=key.key
            )
        ),
    )


def secret_env_from(secret: str) -> TerraformV1Alpha2SpecRunnerPodTemplateSpecEnvFrom:
    return TerraformV1Alpha2SpecRunnerPodTemplateSpecEnvFrom(
        secret_ref=TerraformV1Alpha2SpecRunnerPodTemplateSpecEnvFromSecretRef(name=secret)
    )


def tofu_state_terraform(
    scope: Construct,
    id: str,
    *,
    name: str,
    schema: str,
    source_ref: TerraformV1Alpha2SpecSourceRef,
    path: str,
    interval: str,
    annotations: Mapping[str, str] | None = None,
    approve_plan: str | None = None,
    plan_only: bool | None = None,
    store_readable_plan: TerraformV1Alpha2SpecStoreReadablePlan | None = None,
    targets: Sequence[str] | None = None,
    vars: Sequence[TerraformV1Alpha2SpecVars] | None = None,
    depends_on: Sequence[TerraformV1Alpha2SpecDependsOn] | None = None,
    env: Sequence[TerraformV1Alpha2SpecRunnerPodTemplateSpecEnv] = (),
    env_from: Sequence[TerraformV1Alpha2SpecRunnerPodTemplateSpecEnvFrom] = (),
    node_selector: Mapping[str, str] | None = None,
    volumes: Sequence[TerraformV1Alpha2SpecRunnerPodTemplateSpecVolumes] = (),
    volume_mounts: Sequence[TerraformV1Alpha2SpecRunnerPodTemplateSpecVolumeMounts] = (),
) -> Terraform:
    """A `Terraform` in flux-system, run by `tf-runner`, with its state under `schema` in the
    tofu-state Postgres; the runner reads that database's password as `PGPASSWORD`, ahead of `env`.
    Every plan refreshes first: without it a plan trusts the state and misses reality drifting from
    it (cluster/docs/lessons_learned/2026_04_25_tofu_controller_refresh_before_apply.md). The other
    keywords are `TerraformV1Alpha2Spec` fields under their own names.
    """
    return Terraform(
        scope,
        id,
        metadata=ApiObjectMetadata(name=name, namespace=NAMESPACE, annotations=annotations),
        interval=interval,
        source_ref=source_ref,
        path=path,
        approve_plan=approve_plan,
        plan_only=plan_only,
        refresh_before_apply=True,
        store_readable_plan=store_readable_plan,
        targets=targets,
        service_account_name="tf-runner",
        backend_config=TerraformV1Alpha2SpecBackendConfig(
            custom_configuration=f'backend "pg" {{\n  conn_str    = "{_STATE_DB}"\n  schema_name = "{schema}"\n}}\n'
        ),
        vars=vars,
        depends_on=depends_on,
        runner_pod_template=TerraformV1Alpha2SpecRunnerPodTemplate(
            spec=TerraformV1Alpha2SpecRunnerPodTemplateSpec(
                env_from=list(env_from) or None,
                env=[secret_env("PGPASSWORD", _STATE_DB_PASSWORD), *env],
                node_selector=node_selector,
                resources=_RUNNER_RESOURCES,
                volumes=list(volumes) or None,
                volume_mounts=list(volume_mounts) or None,
            )
        ),
    )


def gitops_terraform(
    scope: Construct,
    id: str,
    *,
    name: str,
    variables: BaseModel | None,
    interval: str = "15m",
    depends_on: Sequence[Terraform] = (),
    env: Sequence[TerraformV1Alpha2SpecRunnerPodTemplateSpecEnv] = (),
    env_from: Sequence[TerraformV1Alpha2SpecRunnerPodTemplateSpecEnvFrom] = (),
    schema: str | None = None,
    store_readable_plan: TerraformV1Alpha2SpecStoreReadablePlan | None = None,
    auto_apply: bool = True,
    node_selector: Mapping[str, str] | None = None,
    volumes: Sequence[TerraformV1Alpha2SpecRunnerPodTemplateSpecVolumes] = (),
    volume_mounts: Sequence[TerraformV1Alpha2SpecRunnerPodTemplateSpecVolumeMounts] = (),
) -> Terraform:
    """The `tf/gitops/<name>` module, run from the `ducktape` GitRepository and, unless
    `auto_apply=False`, auto-approved. Without auto-apply each run plans and waits for a person to
    set `approvePlan` to that plan's id, so drift is reported but nothing changes unasked.
    `name` is the module directory and, underscored, its state schema unless `schema` names the
    one its state already lives in.

    `variables` models the module's variables.tf (None: set none); each field is written
    structurally into the runner's tfvars, so a nested map arrives as a Terraform
    map/object, not a string.

    `interval` controls periodic drift checks; the default is 15 minutes.

    `store_readable_plan=HUMAN` writes each plan's diff to the `tfplan-default-<name>`
    ConfigMap, readable by anyone who can read ConfigMaps in flux-system. Enable it only
    for modules whose providers mark every secret-bearing attribute `Sensitive`, which
    the plan masks.

    `node_selector` pins the runner pod, for a module whose provider reaches something only
    some nodes can (a device on the home LAN). `volumes` and `volume_mounts` add files to the
    runner pod, such as a CA bundle a provider verifies against.
    """
    return tofu_state_terraform(
        scope,
        id,
        name=name,
        schema=schema or name.replace("-", "_"),
        source_ref=TerraformV1Alpha2SpecSourceRef(
            kind=TerraformV1Alpha2SpecSourceRefKind.GIT_REPOSITORY,
            name=ducktape_flux.SOURCE_NAME,
            namespace=flux.NAMESPACE,
        ),
        path=f"./{ducktape_flux.TF_GITOPS_ROOT}/{name}",
        interval=interval,
        approve_plan="auto" if auto_apply else None,
        store_readable_plan=store_readable_plan,
        vars=None
        if variables is None
        else [
            TerraformV1Alpha2SpecVars(name=key, value=value) for key, value in variables.model_dump(mode="json").items()
        ],
        depends_on=[TerraformV1Alpha2SpecDependsOn(name=dependency.name) for dependency in depends_on] or None,
        env=env,
        env_from=env_from,
        node_selector=node_selector,
        volumes=volumes,
        volume_mounts=volume_mounts,
    )
