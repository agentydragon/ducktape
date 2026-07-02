"""Dispatcher settings.

All secrets arrive as env vars from k8s secretKeyRefs (never inline in the pod
spec — pod specs are L0-visible via cluster-diagnostics-reader):

  DATABASE_URL                 — dispatcher database in the haku-dispatch-db CNPG cluster
  WORKERS_LITELLM_MASTER_KEY   — mints per-job virtual keys on the workers-LiteLLM
  ANTHROPIC_API_KEY            — classifier calls (ESO-mirrored haku-cloud key)
  HAKU_API_TOKEN               — bearer Haku presents on /jobs endpoints
  RESULT_TOKEN_SECRET          — HMAC key for per-job result-submission tokens
"""

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings

from haku.dispatch.models import Zone

# Zone wiring. Model allowlists must match the zone key minted in
# tf/gitops/litellm-keys/main.tf and the model_name entries in
# cluster/k8s/haku/dispatch/litellm/workers-litellm-config.yaml.
ZONE_NAMESPACES: dict[Zone, str] = {Zone.ZAI: "haku-sandbox-zai"}
ZONE_MODELS: dict[Zone, set[str]] = {
    Zone.ZAI: {
        f"{m}-anthropic"
        for m in ["glm-4.5", "glm-4.5-air", "glm-4.6", "glm-4.7", "glm-5", "glm-5-turbo", "glm-5.1", "glm-5.2"]
    }
}


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
        default="http://litellm.litellm.svc.cluster.local:4000",
        validation_alias="ANTHROPIC_BASE_URL",
        description="Classifier calls go through the main LiteLLM (Anthropic /v1/messages passthrough).",
    )
    anthropic_api_key: str = Field(
        validation_alias="ANTHROPIC_API_KEY",
        description="LiteLLM virtual key allowlisted to claude-* (litellm-key-dispatcher-classifier).",
    )
    haku_api_token: str = Field(validation_alias="HAKU_API_TOKEN")
    result_token_secret: str = Field(validation_alias="RESULT_TOKEN_SECRET")
    classifier_model: str = Field(default="claude-sonnet-5", validation_alias="CLASSIFIER_MODEL")
    job_template_path: Path = Field(
        default=Path("/etc/dispatcher/job-template.yaml"),
        validation_alias="JOB_TEMPLATE_PATH",
        description="Reviewed k8s Job template (configMapGenerator-mounted from cluster/k8s/haku/dispatch/dispatcher/).",
    )
    job_key_ttl: str = Field(
        default="24h",
        validation_alias="JOB_KEY_TTL",
        description="TTL for per-job LiteLLM keys; matches the Job's activeDeadlineSeconds order of magnitude.",
    )
    host: str = "0.0.0.0"
    port: int = 8000
