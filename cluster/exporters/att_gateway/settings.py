from __future__ import annotations

from enum import StrEnum

from pydantic import Field, HttpUrl, SecretStr
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

    model_config = SettingsConfigDict(env_prefix="ATT_GATEWAY_", case_sensitive=False, extra="ignore", frozen=True)

    url: HttpUrl = Field(description="The gateway's LAN web UI, e.g. `http://192.168.1.254`.")
    access_code: SecretStr = Field(description="The device access code printed on the gateway; opens `syslog.ha`.")
    syslog_server: str = Field(description="Where the gateway should send its firewall log as syslog over UDP.")
    syslog_port: int = Field(default=514, ge=1, le=65535, description="The UDP port at `syslog_server`.")
    syslog_level: SyslogLevel = Field(
        default=SyslogLevel.NOTICE, description="The least severe firewall log level the gateway sends."
    )
    page_gap_seconds: float = Field(
        default=5,
        ge=0,
        description="Pause between two requests. The gateway's web server stalls after a burst of requests.",
    )
    request_timeout_seconds: float = Field(default=45, gt=0, description="Per request.")
