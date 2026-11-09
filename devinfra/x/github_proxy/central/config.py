from pathlib import Path
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, SecretStr, StringConstraints

ClientId = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_-]{0,31}$")]


class ClientCredential(BaseModel):
    model_config = ConfigDict(hide_input_in_errors=True)
    username: ClientId
    password: Annotated[SecretStr, Field(min_length=1)]


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    proxy_hostname: Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9.-]{0,252}$")]
    credential_dirs: list[Path] = Field(min_length=1)
    proxy_tls_cert_file: Path
    proxy_tls_key_file: Path
    interception_ca_cert_file: Path
    interception_ca_key_file: Path
    confdir: Path
    capture_path: Path
    session_ws_events: Path
    listen_host: str = "0.0.0.0"
    listen_port: int = Field(default=8080, ge=0, le=65535)
    metrics_host: str = "0.0.0.0"
    metrics_port: int = Field(default=9090, ge=0, le=65535)
    block_cloud_github_batch: bool = True
    upstream_ca_file: Path | None = None

    def credentials(self) -> dict[str, SecretStr]:
        credentials: dict[str, SecretStr] = {}
        for path in self.credential_dirs:
            incoming = ClientCredential.model_validate(
                {"username": (path / "username").read_text(), "password": (path / "password").read_text()}
            )
            if incoming.username in credentials:
                raise ValueError("Duplicate client IDs across credential directories")
            credentials[incoming.username] = incoming.password
        if len(credentials) > 64:
            raise ValueError("At most 64 proxy clients are supported")
        return credentials
