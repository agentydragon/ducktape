from __future__ import annotations

from pydantic import Field, HttpUrl
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ATT_GATEWAY_", case_sensitive=False, extra="ignore", frozen=True)

    url: HttpUrl = Field(description="The gateway's LAN web UI, e.g. `http://192.168.1.254`.")
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
