"""Dispatcher settings.

All secrets arrive as env vars from k8s secretKeyRefs (never inline in the pod
spec — pod specs are L0-visible via cluster-diagnostics-reader):

  DATABASE_URL                 — dispatcher database in the haku-dispatch-db CNPG cluster
  WORKERS_LITELLM_MASTER_KEY   — mints per-job virtual keys on the workers-LiteLLM
  ANTHROPIC_API_KEY            — classifier virtual key on the main LiteLLM
  HAKU_API_TOKEN               — bearer Haku presents on /jobs endpoints
  RESULT_TOKEN_SECRET          — HMAC key for per-job result-submission tokens

Zone wiring (namespace + model allowlist per zone) is runtime config —
zones.yaml, configMapGenerator-mounted from
cluster/k8s/x/haku/dispatch/dispatcher/ next to the Job template. Its model lists
must match the zone key minted in tf/gitops/litellm-keys/main.tf and the
workers-LiteLLM config (parity-tested in cluster validation).
"""

from pathlib import Path

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings


class ZoneConfig(BaseModel):
    namespace: str = Field(description="Zone namespace validator-stamped Jobs land in.")
    models: set[str] = Field(description="workers-LiteLLM model_name allowlist for this zone's per-job keys.")


def load_zones(path: Path) -> dict[str, ZoneConfig]:
    return {name: ZoneConfig.model_validate(cfg) for name, cfg in yaml.safe_load(path.read_text()).items()}


class Settings(BaseSettings):
    database_url: str = Field(
        validation_alias="DATABASE_URL",
        description="PostgreSQL URL (dispatcher database, haku-dispatch-db-dispatcher secret).",
    )
    workers_litellm_url: str = Field(
        default="http://workers-litellm.haku-dispatch.svc.cluster.local:4000", validation_alias="WORKERS_LITELLM_URL"
    )
    workers_litellm_master_key: str = Field(validation_alias="WORKERS_LITELLM_MASTER_KEY")
    anthropic_base_url: str = Field(
        validation_alias="ANTHROPIC_BASE_URL",
        description="Classifier calls go through the configured Anthropic-compatible backend.",
    )
    anthropic_api_key: str = Field(
        validation_alias="ANTHROPIC_API_KEY", description="Credential accepted by the configured classifier backend."
    )
    haku_api_token: str = Field(validation_alias="HAKU_API_TOKEN")
    result_token_secret: str = Field(validation_alias="RESULT_TOKEN_SECRET")
    classifier_model: str = Field(validation_alias="CLASSIFIER_MODEL", min_length=1)
    job_template_path: Path = Field(
        default=Path("/etc/dispatcher/job-template.yaml"),
        validation_alias="JOB_TEMPLATE_PATH",
        description="Reviewed k8s Job template (configMapGenerator-mounted from cluster/k8s/x/haku/dispatch/dispatcher/).",
    )
    zones_config_path: Path = Field(
        default=Path("/etc/dispatcher/zones.yaml"),
        validation_alias="ZONES_CONFIG_PATH",
        description="Zone wiring (namespace + model allowlist per zone), mounted next to the Job template.",
    )
    classifier_context_path: Path = Field(
        default=Path("/etc/dispatcher-classifier-context/context.md"),
        validation_alias="CLASSIFIER_CONTEXT_PATH",
        description=(
            "Optional operator-provided classifier context (private names/identifiers, known-public "
            "repos), mounted from an optional Secret; missing file = base policy only."
        ),
    )
    job_key_ttl: str = Field(
        default="24h",
        validation_alias="JOB_KEY_TTL",
        description="TTL for per-job LiteLLM keys; matches the Job's activeDeadlineSeconds order of magnitude.",
    )
    host: str = "0.0.0.0"
    port: int = 8000
