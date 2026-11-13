"""Authoring model for the Go egress proxy's deployment configuration.

cdk8s and black-box fixtures serialize these values; the Go process owns loading
flags, environment variables and its YAML file.
"""

from __future__ import annotations

from ipaddress import IPv4Network, IPv6Network
from pathlib import Path
from typing import ClassVar

from pydantic import BaseModel, Field, model_validator
from pydantic_settings import SettingsConfigDict

from agentplane.identity.auth.listener import Listener
from agentplane.log_levels import LogLevels


class APIServer(BaseModel):
    """An API server the proxy acts on by ServiceAccount impersonation."""

    model_config = SettingsConfigDict(extra="forbid")

    host: str = Field(min_length=1, description="The host requests to this API server name.")
    port: int = Field(ge=1, le=65535, description="The port requests to this API server name.")
    token_file: Path = Field(
        description="File holding the proxy's own bearer for this API server, read on every request that "
        "substitutes it so a kubelet-rotated token is used from the next request on."
    )


class Settings(BaseModel):
    """Typed values and environment naming metadata, without runtime settings loading."""

    config_file_env: ClassVar[str] = "AGENTPLANE_EGRESS_CONFIG_FILE"
    model_config = SettingsConfigDict(extra="forbid", env_prefix="AGENTPLANE_EGRESS_")

    rules_namespace: str = Field(
        description="The one namespace holding the PolicySet, PolicyBinding and EgressCredential objects this "
        "proxy enforces. One deployment serves one set of egress policies; a caller's own namespace is unrelated to it."
    )
    credentials_namespace: str = Field(description="Namespace the rules' Secrets are read from.")
    allowed_service_account_namespaces: frozenset[str] = Field(
        min_length=1,
        description="Every namespace whose ServiceAccounts may authenticate here, the sandbox namespace included. "
        "An agent this cluster does not host runs where it runs, so naming its namespace is what lets it present a "
        "token at all; it grants nothing on its own, since a subject no binding names still reaches no rule.",
    )
    tls_passthrough_targets: list[str] = Field(
        default_factory=list,
        description="Exact service authorities carried as opaque TLS; each destination verifies its caller.",
    )
    forward: Listener = Field(description="The one forward listener, transport and accepted hop credentials.")
    admin_listen_address: str = Field(
        default="0.0.0.0:8081",
        description="Shared gRPC listener address serving rules, health and, when configured, EgressAdmin.",
    )
    admin_grpc_audience: str | None = Field(
        default=None,
        min_length=1,
        description="Audience of the bearer accepted by the authenticated admin gRPC API; unset disables that API.",
    )
    admin_grpc_callers: list[str] | None = Field(
        default=None,
        min_length=1,
        description="ServiceAccounts admitted by the admin gRPC API as namespace/name entries; unset disables it.",
    )
    ca_cert: Path = Field(description="PEM certificate of the interception CA the runner containers trust.")
    ca_key: Path = Field(description="PEM private key of the interception CA.")
    upstream_ca_file: Path | None = Field(
        default=None,
        description="PEM bundle this proxy verifies destinations against. Needs the cluster's own CA on "
        "top of the public roots for a `clusterInternal` rule to reach the API server, whose serving "
        "certificate no public root signs. Unset falls back to the image's system roots, which reach "
        "public hosts only.",
    )
    # CLEANUP(added 2026-11-12): remove after staging egress-go-settings is observed without this key.
    projected_token_audiences: list[str] = Field(
        default_factory=list,
        exclude=True,
        description="Compatibility with existing deployment ConfigMaps during rollout; authentication no longer "
        "uses projected workload tokens, and newly rendered settings omit this field.",
    )
    kubeconfig: Path | None = Field(default=None, description="Kubeconfig to use; omit for in-cluster.")
    log_levels: LogLevels

    database_url: str = Field(repr=False, description="Shared diagnostic PostgreSQL database; migrated separately.")
    decision_history_size: int = Field(default=200, ge=1, le=1000)
    decision_retention_days: int = Field(default=7, ge=1, le=365)
    decision_queue_size: int = Field(default=2000, ge=1, le=100000)
    decision_batch_size: int = Field(default=100, ge=1, le=1000)
    local_api_server: APIServer | None = Field(
        default=None,
        description="The `local` API server kubernetesServiceAccount credentials act on. Unset, every such "
        "credential is unavailable. The proxy's RBAC, not this setting, bounds which accounts it may impersonate.",
    )
    exempt_networks: list[IPv4Network | IPv6Network] = Field(
        default_factory=list,
        description="Networks an admitted host may resolve into although they are not globally reachable.",
    )

    @model_validator(mode="after")
    def require_tls(self) -> Settings:
        if self.forward.tls is None:
            raise ValueError("forward listener requires TLS")
        return self
