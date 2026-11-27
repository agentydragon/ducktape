"""TLS contracts and stable workload service identities shared across Agentplane clients."""

from pathlib import Path
from typing import Annotated, Final, Literal, Self
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# The fixed first-party service identities. Each `<PATH>_URI` names `spiffe://agentplane/<path>` and sits in
# exactly one role set below; `render_go_roster` checks both and generates the Go side (`service_identities.go`).
ACTIONS_URI: Final = "spiffe://agentplane/actions"
APP_URI: Final = "spiffe://agentplane/app"
CENTRAL_SAMPLER_URI: Final = "spiffe://agentplane/central-sampler"
EGRESS_PROXY_URI: Final = "spiffe://agentplane/egress-proxy"
ENVIRONMENTS_URI: Final = "spiffe://agentplane/environments"
HISTORY_INGRESS_URI: Final = "spiffe://agentplane/history-ingress"
LLM_INGRESS_URI: Final = "spiffe://agentplane/llm-ingress"
MCP_EDGE_URI: Final = "spiffe://agentplane/mcp-edge"
NOTIFICATIONS_URI: Final = "spiffe://agentplane/notifications"
POLICY_CONTROLLER_URI: Final = "spiffe://agentplane/policy-controller"
# The Runner Gateway in the runner control mutual-TLS trust domain.
RUNNER_GATEWAY_URI: Final = "spiffe://agentplane/runner-gateway"
# `SESSIONS_URI` and `RUNTIME_PROXY_URI` follow Runner Gateway roles across the planned binary split.
RUNTIME_PROXY_URI: Final = "spiffe://agentplane/runtime-proxy"
SANDBOX_CONTROLLER_URI: Final = "spiffe://agentplane/sandbox-controller"
SESSION_DISPATCHER_URI: Final = "spiffe://agentplane/session-dispatcher"
SESSIONS_URI: Final = "spiffe://agentplane/sessions"
# The isolated Runtime-proxy reader of the k3s system-test fixture.
SYSTEM_TEST_RUNTIME_PROXY_READER_URI: Final = "spiffe://agentplane/system-test-runtime-proxy-reader"

# Serve a workload-CA listener (Sandbox and service clients).
WORKLOAD_SERVER_URIS: Final = frozenset(
    {
        ACTIONS_URI,
        CENTRAL_SAMPLER_URI,
        EGRESS_PROXY_URI,
        HISTORY_INGRESS_URI,
        LLM_INGRESS_URI,
        MCP_EDGE_URI,
        NOTIFICATIONS_URI,
        POLICY_CONTROLLER_URI,
        SANDBOX_CONTROLLER_URI,
        SESSION_DISPATCHER_URI,
    }
)
# Serve the runner control listener.
RUNNER_CONTROL_SERVER_URIS: Final = frozenset({RUNNER_GATEWAY_URI})
# Only ever authenticate as clients.
CLIENT_ONLY_URIS: Final = frozenset(
    {APP_URI, ENVIRONMENTS_URI, RUNTIME_PROXY_URI, SESSIONS_URI, SYSTEM_TEST_RUNTIME_PROXY_READER_URI}
)
SERVER_URIS: Final = WORKLOAD_SERVER_URIS | RUNNER_CONTROL_SERVER_URIS
SERVICE_URIS: Final = SERVER_URIS | CLIENT_ONLY_URIS


def _exact_pattern(uris: frozenset[str]) -> str:
    return f"^({'|'.join(sorted(uris))})$"


# Service IDs never use the agent Identity workload grammar.
type ServiceSPIFFEID = Annotated[str, Field(pattern=_exact_pattern(SERVICE_URIS))]


class TLS(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    cert_file: str | None = None
    key_file: str | None = None
    ca_file: str | None = None
    # Replaces `ca_file` for the client certificates this side verifies as a server.
    client_ca_file: str | None = None
    workload_api_socket: str | None = None

    @field_validator("cert_file", "key_file", "ca_file", "client_ca_file")
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
            if any(path is not None for path in (*files, self.client_ca_file)):
                raise ValueError(
                    "workload_api_socket cannot be combined with cert_file, key_file, ca_file, or client_ca_file"
                )
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
    """Verify a backend SVID, optionally authenticating with a service SVID."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    ca_file: Path
    server_uri: str = Field(pattern=_exact_pattern(SERVER_URIS))
    cert_file: Path | None = None
    key_file: Path | None = None
    client_uri: ServiceSPIFFEID | None = None

    @model_validator(mode="after")
    def absolute_path(self) -> Self:
        if not self.ca_file.is_absolute():
            raise ValueError("client TLS requires an absolute ca_file")
        if self.uses_svid and (
            self.cert_file is None
            or self.key_file is None
            or self.client_uri is None
            or not self.cert_file.is_absolute()
            or not self.key_file.is_absolute()
        ):
            raise ValueError("service SVID requires absolute cert_file and key_file paths and a service client_uri")
        return self

    @property
    def uses_svid(self) -> bool:
        return any(value is not None for value in (self.cert_file, self.key_file, self.client_uri))

    def validate_caller(self, token_file: str | Path | None, client_uri: str) -> None:
        if self.uses_svid:
            if token_file:
                raise ValueError("service SVID and bearer token authentication cannot be combined")
            if self.client_uri != client_uri:
                raise ValueError(f"client_uri must be {client_uri!r}")
        elif not token_file:
            raise ValueError("a bearer token file or service SVID is required")


class PolicyServicePeer(BaseModel):
    """A service's `policy_service` block: PolicyService over mutual TLS with the service's SVID.

    Mirrors `identitycache.Peer`; each service's settings check `tls.client_uri` is its own.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    target: str = Field(min_length=1)
    tls: ClientTLS

    @model_validator(mode="after")
    def mutual_tls_to_policy_controller(self) -> Self:
        if not self.tls.uses_svid:
            raise ValueError("policy_service.tls requires the caller's service SVID")
        if self.tls.server_uri != POLICY_CONTROLLER_URI:
            raise ValueError(f"policy_service.tls.server_uri must be {POLICY_CONTROLLER_URI!r}")
        return self


class ServerTLSListener(BaseModel):
    """An additional gRPC listener serving a workload-CA server SVID and bearer-authenticated RPCs."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    listen: str = Field(min_length=1)
    cert_file: Path
    key_file: Path
    client_ca_file: Path | None = None

    @field_validator("cert_file", "key_file", "client_ca_file")
    @classmethod
    def absolute_path(cls, value: Path | None) -> Path | None:
        if value is not None and not value.is_absolute():
            raise ValueError("server TLS certificate and key paths must be absolute")
        return value
