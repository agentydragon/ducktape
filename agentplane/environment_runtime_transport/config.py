"""TLS contracts and stable workload service identities shared across Agentplane clients."""

from pathlib import Path
from typing import Annotated, Final, Literal, Self
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

ACTIONS_URI: Final = "spiffe://agentplane/actions"
EGRESS_PROXY_URI: Final = "spiffe://agentplane/egress-proxy"
HISTORY_SERVICE_URI: Final = "spiffe://agentplane/history-ingress"
LLM_INGRESS_URI: Final = "spiffe://agentplane/llm-ingress"
NOTIFICATIONS_URI: Final = "spiffe://agentplane/notifications"
SESSION_DISPATCHER_URI: Final = "spiffe://agentplane/session-dispatcher"


class TLS(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    cert_file: str | None = None
    key_file: str | None = None
    ca_file: str | None = None
    workload_api_socket: str | None = None

    @field_validator("cert_file", "key_file", "ca_file")
    @classmethod
    def absolute_path(cls, value: str | None) -> str | None:
        if value is not None and not Path(value).is_absolute():
            raise ValueError("Runner control TLS paths must be absolute")
        return value

    @field_validator("workload_api_socket")
    @classmethod
    def unix_socket_uri(cls, value: str | None) -> str | None:
        if value is None:
            return value
        endpoint = urlsplit(value)
        if (
            endpoint.scheme != "unix"
            or endpoint.netloc
            or not Path(endpoint.path).is_absolute()
            or endpoint.query
            or endpoint.fragment
            or value != f"unix://{endpoint.path}"
        ):
            raise ValueError("workload_api_socket must be a unix:///absolute/path URI")
        return value

    @model_validator(mode="after")
    def one_source(self) -> Self:
        files = (self.cert_file, self.key_file, self.ca_file)
        if self.workload_api_socket is not None:
            if any(path is not None for path in files):
                raise ValueError("workload_api_socket cannot be combined with cert_file, key_file, or ca_file")
        elif any(path is None for path in files):
            raise ValueError("cert_file, key_file, and ca_file are all required without workload_api_socket")
        return self


class MTLS(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    mode: Literal["mtls"] = "mtls"
    tls: TLS


class Insecure(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    mode: Literal["insecure"] = "insecure"


Security = Annotated[MTLS | Insecure, Field(discriminator="mode")]


class ClientTLS(BaseModel):
    """Verify a backend service SVID without presenting a client certificate."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    ca_file: Path
    server_uri: str = Field(
        pattern=r"^spiffe://agentplane/(runner-gateway|egress-proxy|history-ingress|notifications|llm-ingress|session-dispatcher|actions)$"
    )

    @model_validator(mode="after")
    def absolute_path(self) -> Self:
        if not self.ca_file.is_absolute():
            raise ValueError("client TLS requires an absolute ca_file")
        return self
