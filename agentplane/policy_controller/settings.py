"""Authoring schema for the Go Policy Controller's deployment settings."""

from __future__ import annotations

from collections.abc import Set

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Fields are `AGENTPLANE_POLICY_CONTROLLER_*` environment variables; a set is written as its elements joined
    by commas. Keep them in sync with the keys of `cmd/config.go`."""

    model_config = SettingsConfigDict(env_prefix="AGENTPLANE_POLICY_CONTROLLER_")

    policy_namespace: str = Field(min_length=1, description="Namespace where the PolicySets and PolicyBindings live.")
    subject_namespaces: Set[str] = Field(
        min_length=1, description="Namespaces whose ServiceAccounts a PolicyBinding may bind."
    )
    accounts_namespace: str = Field(
        min_length=1,
        description=(
            "Namespace where ManagedServiceAccounts' ServiceAccounts are created; ServiceAccountGrants may name "
            "accounts here as well as in subject_namespaces."
        ),
    )
    metrics_bind_address: str = Field(default=":8080", min_length=1, description="Where Prometheus metrics are served.")
    health_probe_bind_address: str = Field(
        default=":8081", min_length=1, description="Where /healthz and /readyz are served."
    )
    grpc_bind_address: str = Field(default=":8082", min_length=1, description="PolicyService gRPC listener.")
    grpc_audience: str = Field(default="agentplane-policy", min_length=1, description="PolicyService token audience.")
    reader_accounts: Set[str] = Field(
        default_factory=frozenset, description="Accounts allowed to read all policy resources."
    )
    writer_accounts: Set[str] = Field(
        default_factory=frozenset, description="Accounts allowed to create and revoke operator PolicyBindings."
    )
