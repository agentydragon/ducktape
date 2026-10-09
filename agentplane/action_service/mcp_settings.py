"""Configured remote MCP servers and their shared OAuth client identity."""

from __future__ import annotations

from pathlib import Path

from pydantic import (
    AnyHttpUrl,
    BaseModel,
    ConfigDict,
    Field,
    ValidatorFunctionWrapHandler,
    field_validator,
    model_validator,
)

from agentplane.action_service.catalog import Key
from util.urls import HttpsUrl


class McpOAuthServer(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    server_id: Key
    server_url: str = Field(min_length=1)
    authorization_endpoint: str | None = None
    token_endpoint: str | None = None
    client_id: str | None = Field(default=None, min_length=1)
    use_shared_cimd: bool = False
    client_secret_file: Path | None = None
    redirect_uri: str = Field(min_length=1)
    scopes: list[str] = Field(default_factory=list)
    resource: str | None = None

    @model_validator(mode="after")
    def validate_client_configuration(self) -> McpOAuthServer:
        if (self.client_id is None) != self.use_shared_cimd:
            raise ValueError("configure exactly one of client_id or use_shared_cimd")
        if self.use_shared_cimd and self.client_secret_file is not None:
            raise ValueError("CIMD clients use public token authentication and cannot have a client secret")
        return self


class McpClientMetadataSettings(BaseModel):
    """The one public OAuth client identity shared by MCP linkages in this deployment."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    url: HttpsUrl
    client_name: str = Field(min_length=1)

    @field_validator("url", mode="wrap")
    @classmethod
    def validate_url(cls, value: object, handler: ValidatorFunctionWrapHandler) -> AnyHttpUrl:
        url: AnyHttpUrl = handler(value)
        if url.port != 443 or url.path != "/oauth/client-metadata.json":
            raise ValueError("mcp_client_metadata.url must be an HTTPS URL at /oauth/client-metadata.json")
        if isinstance(value, str) and str(url) != value:
            raise ValueError("mcp_client_metadata.url must use its canonical URL form")
        return url
