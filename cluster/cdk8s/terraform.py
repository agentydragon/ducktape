"""The tofu-controller `Terraform` CR every `tf/gitops/<name>` module shares: run from the
`ducktape` GitRepository by `tf-runner`, auto-approved, state in the OVH tofu-state
Postgres under the module's own schema."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from constructs import Construct
from tofu_controller.io.fluxcd.contrib.infra import (
    TerraformV1Alpha2,
    TerraformV1Alpha2Spec,
    TerraformV1Alpha2SpecBackendConfig,
    TerraformV1Alpha2SpecDependsOn,
    TerraformV1Alpha2SpecRunnerPodTemplate,
    TerraformV1Alpha2SpecRunnerPodTemplateSpec,
    TerraformV1Alpha2SpecRunnerPodTemplateSpecEnv,
    TerraformV1Alpha2SpecRunnerPodTemplateSpecEnvFrom,
    TerraformV1Alpha2SpecRunnerPodTemplateSpecEnvFromSecretRef,
    TerraformV1Alpha2SpecRunnerPodTemplateSpecEnvValueFrom,
    TerraformV1Alpha2SpecRunnerPodTemplateSpecEnvValueFromSecretKeyRef,
    TerraformV1Alpha2SpecSourceRef,
    TerraformV1Alpha2SpecSourceRefKind,
    TerraformV1Alpha2SpecVars,
)

from cluster.cdk8s.metadata import metadata

NAMESPACE = "flux-system"
_STATE_DB = "postgres://tfstate@tofu-state-db-ovh-rw.tofu-state.svc:5432/tfstate?sslmode=disable"


def secret_env(name: str, secret: str, key: str) -> TerraformV1Alpha2SpecRunnerPodTemplateSpecEnv:
    return TerraformV1Alpha2SpecRunnerPodTemplateSpecEnv(
        name=name,
        value_from=TerraformV1Alpha2SpecRunnerPodTemplateSpecEnvValueFrom(
            secret_key_ref=TerraformV1Alpha2SpecRunnerPodTemplateSpecEnvValueFromSecretKeyRef(name=secret, key=key)
        ),
    )


def secret_env_from(secret: str) -> TerraformV1Alpha2SpecRunnerPodTemplateSpecEnvFrom:
    return TerraformV1Alpha2SpecRunnerPodTemplateSpecEnvFrom(
        secret_ref=TerraformV1Alpha2SpecRunnerPodTemplateSpecEnvFromSecretRef(name=secret)
    )


def gitops_terraform(
    scope: Construct,
    id: str,
    *,
    name: str,
    variables: Mapping[str, Any],
    depends_on: Sequence[str] = (),
    env: Sequence[TerraformV1Alpha2SpecRunnerPodTemplateSpecEnv] = (),
    env_from: Sequence[TerraformV1Alpha2SpecRunnerPodTemplateSpecEnvFrom] = (),
) -> TerraformV1Alpha2:
    """`name` is the module directory under tf/gitops and, underscored, its state schema.

    `variables` values are written structurally into the runner's tfvars, so a nested
    map arrives as a Terraform map/object, not a string.
    """
    return TerraformV1Alpha2(
        scope,
        id,
        metadata=metadata(name, NAMESPACE),
        spec=TerraformV1Alpha2Spec(
            interval="15m",
            refresh_before_apply=True,
            path=f"./tf/gitops/{name}",
            source_ref=TerraformV1Alpha2SpecSourceRef(
                kind=TerraformV1Alpha2SpecSourceRefKind.GIT_REPOSITORY, name="ducktape", namespace="ducktape-flux"
            ),
            service_account_name="tf-runner",
            approve_plan="auto",
            backend_config=TerraformV1Alpha2SpecBackendConfig(
                custom_configuration=(
                    f'backend "pg" {{\n  conn_str    = "{_STATE_DB}"\n  schema_name = "{name.replace("-", "_")}"\n}}\n'
                )
            ),
            vars=[TerraformV1Alpha2SpecVars(name=key, value=value) for key, value in variables.items()] or None,
            depends_on=[TerraformV1Alpha2SpecDependsOn(name=dep) for dep in depends_on] or None,
            runner_pod_template=TerraformV1Alpha2SpecRunnerPodTemplate(
                spec=TerraformV1Alpha2SpecRunnerPodTemplateSpec(
                    env_from=list(env_from) or None,
                    env=[secret_env("PGPASSWORD", "tofu-state-db-credentials", "password"), *env],
                )
            ),
        ),
    )
