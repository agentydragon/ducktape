"""Action approval Web Push delivery configuration."""

from pydantic import BaseModel, Field, SecretStr


class WebPushSettings(BaseModel):
    private_key_pem: SecretStr = Field(min_length=1)
    subject: str = Field(min_length=1)
    public_base_url: str = Field(min_length=1)
    allowed_push_hosts: frozenset[str] = Field(
        min_length=1, description="Exact reviewed browser push-service hostnames; no arbitrary callback destinations."
    )
