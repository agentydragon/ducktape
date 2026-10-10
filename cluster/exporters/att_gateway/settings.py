from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class SyslogLevel(StrEnum):
    """The firewall log severities the gateway's `syslog.ha` offers, most severe first; a
    level sends itself and everything above it."""

    EMERGENCY = "Emergency"
    ALERT = "Alert"
    CRITICAL = "Critical"
    ERROR = "Error"
    WARNING = "Warning"
    NOTICE = "Notice"


class Syslog(BaseModel):
    """Where the gateway sends its firewall log (the rows `logs.ha` shows) as syslog over UDP:
    its `syslog.ha` form."""

    model_config = ConfigDict(frozen=True)

    enabled: bool
    server: str = Field(description="Host the gateway sends to; empty on a gateway that never had one set.")
    port: int = Field(ge=1, le=65535)
    level: SyslogLevel = Field(description="The least severe firewall log level the gateway sends.")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ATT_GATEWAY_", case_sensitive=False, extra="ignore", frozen=True)

    url: HttpUrl = Field(description="The gateway's LAN web UI, e.g. `http://192.168.1.254`.")
    access_code: SecretStr | None = Field(
        default=None,
        description="The device access code printed on the gateway. Unset, the pages behind it are not polled.",
    )
    poll_interval_seconds: float = Field(
        default=60, gt=0, description="Start of one poll of every page to the start of the next."
    )
    page_gap_seconds: float = Field(
        default=5,
        ge=0,
        description="Pause between two page fetches. The gateway's web server stalls after a burst of requests.",
    )
    request_timeout_seconds: float = Field(
        default=45, gt=0, description="Per page; a healthy gateway sometimes takes 20 s to render `fiberstat`."
    )
    listen_port: int = Field(default=9173, description="Port serving `/metrics`.")


class SyslogSettings(BaseSettings):
    """`syslog_reconciler`'s: where the gateway should send its firewall log."""

    model_config = SettingsConfigDict(
        env_prefix="ATT_GATEWAY_", env_nested_delimiter="__", case_sensitive=False, extra="ignore", frozen=True
    )

    url: HttpUrl = Field(description="The gateway's LAN web UI, e.g. `http://192.168.1.254`.")
    access_code: SecretStr = Field(description="The device access code printed on the gateway; opens `syslog.ha`.")
    syslog: Syslog = Field(description="The setting the gateway should have.")
    page_gap_seconds: float = Field(
        default=5,
        ge=0,
        description="Pause between two requests. The gateway's web server stalls after a burst of requests.",
    )
    request_timeout_seconds: float = Field(default=45, gt=0, description="Per request.")
