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

from agentplane.log_levels import LogLevels

PROXY_MTLS_PORT = 8889
RULES_WORKLOAD_PORT = 8443
RULES_WORKLOAD_URI = "spiffe://agentplane/egress-rules"


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
        description="Sandbox namespaces whose SVIDs and destination-scoped projected tokens may authenticate here. "
        "The live Sandbox selects its own ServiceAccount; namespace admission grants no policy authority.",
    )
    svid_listen_address: str = Field(description="Required mTLS forward-proxy listener address.")
    svid_cert_file: Path = Field(description="Server certificate for the SVID listener.")
    svid_key_file: Path = Field(description="Private key for the SVID listener certificate.")
    svid_ca_file: Path = Field(description="Trust bundle for SVID client certificates.")
    rules_listen_address: str = Field(description="Required workload-SVID TLS listener for the Rules API.")
    rules_cert_file: Path = Field(description="Rules API serving certificate.")
    rules_key_file: Path = Field(description="Private key for the Rules API serving certificate.")
    rules_ca_file: Path = Field(description="Trust bundle for Sandbox SVID clients of the Rules API.")
    admin_listen_address: str = Field(
        default="0.0.0.0:8081",
        description="Admin listener address serving gRPC health and, when configured, the EgressAdmin API.",
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
    projected_token_audiences: frozenset[str] = Field(
        default=frozenset(),
        description="Every audience a rule may substitute a sidecar's projected token for, the API server's "
        "among them. Reviewing the list here is what decides which destinations this proxy will spend a "
        "caller's own identity on; a credential naming an audience absent from it resolves to nothing.",
    )
    tls_passthrough_targets: list[str] = Field(
        default_factory=list,
        description="Exact host:port authorities tunneled as opaque TLS after SVID hop authentication. "
        "Each destination verifies the workload certificate and authorizes its own requests.",
    )
    kubeconfig: Path | None = Field(default=None, description="Kubeconfig to use; omit for in-cluster.")
    log_levels: LogLevels

    database_url: str = Field(repr=False, description="Shared diagnostic PostgreSQL database; migrated separately.")
    decision_history_size: int = Field(default=200, ge=1, le=1000)
    decision_retention_days: int = Field(default=7, ge=1, le=365)
    decision_queue_size: int = Field(default=2000, ge=1, le=100000)
    decision_batch_size: int = Field(default=100, ge=1, le=1000)
    exempt_networks: list[IPv4Network | IPv6Network] = Field(
        default_factory=list,
        description="Networks an admitted host may resolve into although they are not globally reachable.",
    )

    @model_validator(mode="after")
    def tls_listeners_are_configured(self) -> Settings:
        if any(
            not path.is_absolute()
            for path in (
                self.svid_cert_file,
                self.svid_key_file,
                self.svid_ca_file,
                self.rules_cert_file,
                self.rules_key_file,
                self.rules_ca_file,
            )
        ):
            raise ValueError("SVID and rules TLS file paths must be absolute")
        return self
