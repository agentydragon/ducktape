"""Workload-facing LLM ingress model configuration contract."""

from pydantic import BaseModel, ConfigDict, Field


class ModelConfig(BaseModel):
    """Agentplane client policy for an exposed route, not verified provider capacity."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    # TODO(#9574): separate the exposed route ID from the LiteLLM model ID for harnesses
    # that recognize specific slugs. Coordinate inference request/response translation
    # with this lookup; do not rename routes or infer client policy from names here.
    model: str = Field(min_length=1, description="Exposed model route ID, not the backend's model slug.")
    total_context_budget_tokens: int = Field(
        gt=0,
        strict=True,
        description=(
            "Configured total context budget for the harness: input and output share this budget. "
            "Not an independent input limit, output limit, or verified provider capacity. "
            "Harness-specific reserves and compaction behavior still apply."
        ),
    )
