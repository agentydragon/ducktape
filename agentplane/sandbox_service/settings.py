"""Sandbox Service deployment settings and configuration file name."""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

from pydantic import Field
from pydantic_settings import SettingsConfigDict

from agentplane.sandbox_service.kubernetes_grants import KubernetesGrant
from agentplane.sandbox_service.provisioning import OwnSandboxes
from agentplane.sandbox_service.runner_endpoints import RunnerEndpoint
from agentplane.subjects import ServiceAccountRef
from util.yaml_file_settings import YamlFileSettings


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
    default_egress_policies: list[str] = Field(default_factory=list)
    own_sandboxes: OwnSandboxes | None = Field(
        default=None, description="Create Agentplane's own Sandbox kind; absent, agent-sandbox's."
    )
    kubernetes_grants: dict[str, KubernetesGrant] = Field(default_factory=dict)
    kubernetes_binding_cleanup_namespaces: set[str] = Field(default_factory=set)
    kubernetes_cluster_binding_cleanup: bool = False
    runner_control: RunnerEndpoint = Field(
        description="The Runner Gateway endpoint every new Sandbox's runner dials for its control stream."
    )
    runner_ingress: RunnerEndpoint = Field(description="The ingress every new Sandbox's runner pushes its Events to.")
    control_target: str = Field(
        min_length=1,
        description="A Sandbox's control Service, which reaches the gateway replica holding its runner connection, "
        "as a template of `{sandbox}` and `{namespace}`.",
    )
    gateway_token_file: Path = Field(
        description="This Pod's projected token for the gateway's audience; the gateway accepts it from the "
        "Sandbox Service alone for `SealSandbox`."
    )
