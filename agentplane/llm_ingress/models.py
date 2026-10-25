"""Workload-facing LLM ingress model configuration contract."""

import re
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator


class VerifiedClaim(StrEnum):
    """Fields supplied by the authenticated Kubernetes principal."""

    NAMESPACE = "namespace"
    SERVICE_ACCOUNT = "service_account"
    POD_NAME = "pod_name"
    POD_UID = "pod_uid"


class AttributionField(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    key: str = Field(min_length=1, description="Literal JSON member or W3C baggage key.")
    source: VerifiedClaim = Field(description="Verified principal field supplying the value.")


class AttributionJsonHeader(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(description="HTTP header name receiving a JSON object.")
    fields: list[AttributionField] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_header(self) -> AttributionJsonHeader:
        lower = self.name.lower()
        if (
            not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", self.name)
            or not lower.startswith("x-")
            or lower
            in {
                "x-api-key",
                "x-litellm-api-key",
                "x-real-ip",
                "x-http-method-override",
                "x-litellm-agent-id",
                "x-litellm-customer-id",
            }
            or lower.startswith(("x-forwarded-", "x-original-"))
        ):
            raise ValueError(f"invalid attribution JSON header: {self.name}")
        if len({field.key for field in self.fields}) != len(self.fields):
            raise ValueError(f"duplicate attribution JSON key in {self.name}")
        return self


class AttributionConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    json_headers: list[AttributionJsonHeader] = Field(default_factory=list)
    baggage: list[AttributionField] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_outputs(self) -> AttributionConfig:
        if not self.json_headers and not self.baggage:
            raise ValueError("attribution must configure at least one output")
        if len({header.name.lower() for header in self.json_headers}) != len(self.json_headers):
            raise ValueError("duplicate attribution JSON header")
        if len({field.key for field in self.baggage}) != len(self.baggage):
            raise ValueError("duplicate attribution baggage key")
        if any(not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", field.key) for field in self.baggage):
            raise ValueError("invalid attribution baggage key")
        return self


def default_attribution() -> AttributionConfig:
    return AttributionConfig(
        json_headers=[
            AttributionJsonHeader(
                name="x-litellm-spend-logs-metadata",
                fields=[
                    AttributionField(key="agentplane.namespace", source=VerifiedClaim.NAMESPACE),
                    AttributionField(key="agentplane.service_account", source=VerifiedClaim.SERVICE_ACCOUNT),
                    AttributionField(key="agentplane.pod_name", source=VerifiedClaim.POD_NAME),
                    AttributionField(key="agentplane.pod_uid", source=VerifiedClaim.POD_UID),
                ],
            )
        ]
    )


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
