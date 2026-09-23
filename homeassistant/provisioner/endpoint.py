"""Where the provisioners reach Home Assistant, and the OAuth client their login flows present."""

from __future__ import annotations

from pydantic import BaseModel


class HomeAssistantEndpoint(BaseModel):
    """Home Assistant's API base URL and the OAuth client identity its login flows use."""

    url: str
    client_id: str
    redirect_uri: str
