"""Egress Proxy deployment settings and configuration file name."""

from __future__ import annotations

import os
from ipaddress import IPv4Network, IPv6Network
from pathlib import Path
from typing import Any

from pydantic import Field
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict, YamlConfigSettingsSource

# YamlConfigSettingsSource loads yaml lazily inside pydantic-settings; gazelle cannot see the dependency.
# gazelle:include_dep @pypi//pyyaml


# Names the YAML settings file a deployment mounts; not a field, so not a flag.
CONFIG_FILE_ENV = "AGENTPLANE_EGRESS_CONFIG_FILE"


class Settings(BaseSettings):
    """Each field is a `--flag` and an `AGENTPLANE_EGRESS_*` environment variable."""

    model_config = SettingsConfigDict(env_prefix="AGENTPLANE_EGRESS_", cli_parse_args=True, cli_kebab_case=True)

    rules_namespace: str = Field(
        description="The one namespace holding the EgressPolicy, EgressBinding and EgressCredential objects this "
        "proxy enforces. One deployment serves one egress-policy set; a caller's own namespace is unrelated to it."
    )
    credentials_namespace: str = Field(description="Namespace the rules' Secrets are read from.")
    allowed_service_account_namespaces: frozenset[str] = Field(
        min_length=1,
        description="Every namespace whose ServiceAccounts may authenticate here, the sandbox namespace included. "
        "An agent this cluster does not host runs where it runs, so naming its namespace is what lets it present a "
        "token at all; it grants nothing on its own, since a subject no binding names still reaches no rule.",
    )
    listen_host: str = Field(default="0.0.0.0", description="Proxy listener bind address.")
    listen_port: int = Field(default=8888, description="Proxy listener port the sidecars relay to.")
    admin_host: str = Field(default="0.0.0.0", description="Admin listener bind address.")
    admin_port: int = Field(default=8081, description="Admin port serving /decisions and /healthz.")
    agent_api_host: str = Field(default="0.0.0.0", description="Agent-facing rules API bind address.")
    agent_api_port: int = Field(default=8082, description="Agent-facing HTTP port serving /v1/rules.")
    ca_cert: Path = Field(description="PEM certificate of the interception CA the runner containers trust.")
    ca_key: Path = Field(description="PEM private key of the interception CA.")
    confdir: Path = Field(description="Writable directory mitmproxy keeps its CA and issued leaves in.")
    upstream_ca_file: Path | None = Field(
        default=None,
        description="PEM bundle this proxy verifies destinations against. Needs the cluster's own CA on "
        "top of the public roots for a `clusterInternal` rule to reach the API server, whose serving "
        "certificate no public root signs. Unset falls back to mitmproxy's bundled roots, which reach "
        "public hosts only.",
    )
    token_audience: str = Field(default="agentplane-egress", description="Audience of the sidecars' projected tokens.")
    projected_token_audiences: frozenset[str] = Field(
        default=frozenset(),
        description="Every audience a rule may substitute a sidecar's projected token for, the API server's "
        "among them. Reviewing the list here is what decides which destinations this proxy will spend a "
        "caller's own identity on; a credential naming an audience absent from it resolves to nothing.",
    )
    kubeconfig: Path | None = Field(default=None, description="Kubeconfig to use; omit for in-cluster.")

    resync_seconds: int = Field(default=300, gt=0, description="Watch lifetime; every kind is relisted this often.")
    database_url: str = Field(repr=False, description="Shared diagnostic PostgreSQL database; migrated separately.")
    decision_history_size: int = Field(default=200, ge=1, le=1000)
    decision_retention_days: int = Field(default=7, ge=1, le=365)
    decision_queue_size: int = Field(default=2000, ge=1, le=100000)
    decision_batch_size: int = Field(default=100, ge=1, le=1000)
    decision_flush_seconds: float = Field(default=5, gt=0, le=20)
    exempt_networks: list[IPv4Network | IPv6Network] = Field(
        default_factory=list,
        description="Networks an admitted host may resolve into although they are not globally reachable.",
    )

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        sources: list[PydanticBaseSettingsSource] = [init_settings, env_settings, dotenv_settings]
        if config_file := os.environ.get(CONFIG_FILE_ENV):
            # pydantic-settings silently ignores absent YAML files. An explicit deployment binding
            # must never turn into a healthy service running on defaults.
            if not Path(config_file).is_file():
                raise ValueError("configured egress proxy settings file is not a regular file")
            sources.append(YamlConfigSettingsSource(settings_cls, yaml_file=config_file))
        sources.append(file_secret_settings)
        return tuple(sources)

    def __init__(self, **values: Any) -> None:
        # BaseSettings fills required fields from its sources; spell that out because the mypy plugin
        # derives a required-argument signature from the fields.
        super().__init__(**values)
