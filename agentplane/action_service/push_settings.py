"""Action approval Web Push delivery configuration."""

from pydantic import BaseModel, ConfigDict, Field, field_serializer


class WebPushSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    subject: str = Field(min_length=1)
    public_base_url: str = Field(min_length=1)
    allowed_push_hosts: frozenset[str] = Field(
        min_length=1, description="Exact reviewed browser push-service hostnames; no arbitrary callback destinations."
    )

    @field_serializer("allowed_push_hosts")
    def serialize_hosts(self, hosts: frozenset[str]) -> list[str]:
        return sorted(hosts)
