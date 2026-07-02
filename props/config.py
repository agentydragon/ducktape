"""Props configuration loaded from TOML file.

The config file path is specified by the PROPS_CONFIG_FILE environment variable.
Loaded at boundaries (CLI entry points, backend lifespan, test fixtures) and
passed explicitly — no singletons.

## Configuration Sources

Model routing uses data from two sources:

1. **Database (model_metadata table)**:
   - All model pricing/limits (input_usd_per_1m_tokens, context_window_tokens, etc.)
   - Upstream routing pointers (upstream_name, upstream_model)
   - Synced via `props db recreate` from:
     - openai_utils/model_metadata.yaml (OpenAI models, upstream_name=NULL)
     - PropsConfig.models (custom models, upstream_name/upstream_model from config)

2. **Config file (PROPS_CONFIG_FILE)**:
   - Upstream definitions (upstreams.*): URL and API key env var names
   - Custom model definitions (models): synced to DB during recreate

The database stores *which* upstream a model uses. The config file stores *how*
to connect to that upstream (URL, API key). This separation allows:
- Database queries for cost calculation (pricing data in DB)
- Runtime flexibility for upstream credentials (env vars in config)
- Custom models defined in config but synced to DB for FK consistency
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Discriminator, Field, model_validator

from openai_utils.api_shape import LLMApiShape
from openai_utils.model_metadata import ModelMetadata as BaseModelMetadata

ENV_CONFIG_FILE = "PROPS_CONFIG_FILE"


class UpstreamConfig(BaseModel):
    """Configuration for an LLM upstream provider.

    Either url (static) or url_env (from environment) must be set.
    """

    model_config = ConfigDict(frozen=True)

    url: str | None = None
    url_env: str | None = None
    api_key_env: str

    @model_validator(mode="after")
    def check_url_or_url_env(self) -> UpstreamConfig:
        if self.url is None and self.url_env is None:
            raise ValueError("Either url or url_env must be set")
        if self.url is not None and self.url_env is not None:
            raise ValueError("Cannot set both url and url_env")
        return self


class CustomModelConfig(BaseModelMetadata):
    """Custom model configuration for non-OpenAI upstreams.

    Inherits pricing/limits fields from openai_utils.ModelMetadata.
    Adds upstream routing info (name, upstream reference, upstream model name).
    """

    model_config = ConfigDict(frozen=True)

    name: str
    upstream: str
    upstream_model: str
    api_shape: LLMApiShape = LLMApiShape.RESPONSES
    max_parallel_agents: int | None = None


class DockerExecutorConfig(BaseModel):
    """Configuration for the Docker container executor."""

    model_config = ConfigDict(frozen=True)

    type: Literal["docker"] = "docker"
    extra_hosts: dict[str, str] = Field(default_factory=dict)


class KubernetesExecutorConfig(BaseModel):
    """Configuration for the Kubernetes container executor."""

    model_config = ConfigDict(frozen=True)

    type: Literal["kubernetes"] = "kubernetes"
    namespace: str
    kubeconfig: str | None = None
    image_pull_secret: str | None = None


ExecutorConfig = Annotated[DockerExecutorConfig | KubernetesExecutorConfig, Discriminator("type")]


class LLMProxyConfig(BaseModel):
    """Config the standalone LLM proxy reads from the config file: just the
    upstream provider definitions (per-upstream URL + API-key env var).

    The proxy's DB comes from `PG*` env vars and its model routing from the DB
    `model_metadata` table (synced by the API server), so `upstreams` is the only
    config-file field it needs. `PropsConfig` (the API server's config) extends
    this with the API-only fields, which the proxy never reads.
    """

    model_config = ConfigDict(frozen=True)

    upstreams: dict[str, UpstreamConfig] = {}


class PropsConfig(LLMProxyConfig):
    """Full config for the API server: the shared `upstreams` plus API-only fields."""

    backend_url: str
    # Base URL agents use for the LLM proxy (split out of the backend); agents get
    # OPENAI_BASE_URL = <llm_proxy_url>/v1. Required for agent runs: the backend
    # raises at startup if it's None (there is no backend_url fallback).
    llm_proxy_url: str | None = None
    grader_model: str | None = None
    agent_env: dict[str, str]
    models: list[CustomModelConfig] = []
    executor: ExecutorConfig = Field(default_factory=DockerExecutorConfig)
    auto_migrate: bool = False
    auto_sync_specimens: bool = False
    # Dev-only conveniences (never enable in the cluster deployment): logs the
    # admin token and a ready-to-use admin URL at startup.
    dev_mode: bool = False


def load_config(path: Path) -> PropsConfig:
    """Load props configuration from a TOML file."""
    data = tomllib.loads(path.read_text())
    return PropsConfig.model_validate(data)


def load_config_from_env() -> PropsConfig:
    """Load props configuration from the path in PROPS_CONFIG_FILE env var."""
    path_str = os.environ.get(ENV_CONFIG_FILE)
    if not path_str:
        raise ValueError(f"{ENV_CONFIG_FILE} environment variable not set")
    return load_config(Path(path_str))


def load_proxy_config(path: Path) -> LLMProxyConfig:
    """Load just the LLM-proxy slice (`upstreams`) from a TOML file; the API-only
    fields in the same file are ignored."""
    return LLMProxyConfig.model_validate(tomllib.loads(path.read_text()))


def load_proxy_config_from_env() -> LLMProxyConfig:
    """Load the LLM-proxy config from the path in PROPS_CONFIG_FILE env var."""
    path_str = os.environ.get(ENV_CONFIG_FILE)
    if not path_str:
        raise ValueError(f"{ENV_CONFIG_FILE} environment variable not set")
    return load_proxy_config(Path(path_str))
