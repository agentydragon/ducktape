"""Kubernetes Policy Controller deployment settings and configuration file name."""

from __future__ import annotations

from collections.abc import Set
from pathlib import Path
from typing import ClassVar

from pydantic import Field
from pydantic_settings import SettingsConfigDict

from util.yaml_file_settings import YamlFileSettings


class Settings(YamlFileSettings):
    config_file_env: ClassVar[str] = "AGENTPLANE_KUBERNETES_POLICY_CONTROLLER_CONFIG_FILE"
    model_config = SettingsConfigDict(
        env_prefix="AGENTPLANE_KUBERNETES_POLICY_CONTROLLER_", cli_parse_args=True, cli_kebab_case=True
    )

    policy_namespace: str = Field(
        min_length=1,
        description="Where the PolicySets and PolicyBindings live; also labels every native binding this "
        "controller keeps, so controllers of other Agentplane instances leave them alone.",
    )
    subject_namespaces: Set[str] = Field(
        min_length=1, description="The namespaces whose ServiceAccounts a PolicyBinding may name."
    )
    resync_seconds: int = Field(default=300, gt=0)
    kubeconfig: Path | None = None
