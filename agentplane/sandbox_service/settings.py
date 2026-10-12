"""Sandbox Service deployment settings and configuration file name."""

from __future__ import annotations

from collections.abc import Set
from pathlib import Path
from typing import ClassVar, Self

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator
from pydantic_settings import SettingsConfigDict

from agentplane.subjects import ServiceAccountRef
from util.yaml_file_settings import YamlFileSettings


# CLEANUP(added 2026-10-12): drop once the manifests set default_policy_sets (the cdk8s PR after this one).
class OwnSandboxes(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    default_policy_sets: Set[str]


class Settings(YamlFileSettings):
    config_file_env: ClassVar[str] = "AGENTPLANE_SANDBOX_SERVICE_CONFIG_FILE"
    model_config = SettingsConfigDict(
        env_prefix="AGENTPLANE_SANDBOX_SERVICE_", cli_parse_args=True, cli_kebab_case=True
    )

    sandbox_namespace: str = Field(min_length=1)
    caller_accounts: frozenset[ServiceAccountRef] = Field(min_length=1)
    token_audience: str = Field(min_length=1, description="Audience of the callers' projected tokens.")
    admission_timeout_s: float = Field(default=15, gt=0, le=60)
    lifecycle_timeout_s: float = Field(default=300, gt=0)
    host: str = "0.0.0.0"
    port: int = Field(default=8080, ge=1, le=65535)
    kubeconfig: Path | None = None
    # CLEANUP(added 2026-10-12): required, and own_sandboxes and the validator below gone, once the
    #   manifests set default_policy_sets (the cdk8s PR after this one).
    default_policy_sets: Set[str] | None = Field(default=None, description="PolicySets every new Sandbox is bound to.")
    own_sandboxes: OwnSandboxes | None = None
    # CLEANUP(added 2026-10-12): drop once the manifests stop setting these (the cdk8s PR after this one).
    default_egress_policies: JsonValue | None = None
    kubernetes_grants: JsonValue | None = None
    kubernetes_binding_cleanup_namespaces: JsonValue | None = None
    kubernetes_cluster_binding_cleanup: JsonValue | None = None
    runner_control: JsonValue | None = None
    runner_ingress: JsonValue | None = None
    control_target: JsonValue | None = None
    gateway_token_file: JsonValue | None = None

    @model_validator(mode="after")
    def _one_source_of_default_policy_sets(self) -> Self:
        if (self.default_policy_sets is None) == (self.own_sandboxes is None):
            raise ValueError("set exactly one of default_policy_sets and own_sandboxes.default_policy_sets")
        return self

    @property
    def policy_sets_of_every_sandbox(self) -> Set[str]:
        if self.default_policy_sets is not None:
            return self.default_policy_sets
        assert self.own_sandboxes is not None
        return self.own_sandboxes.default_policy_sets
