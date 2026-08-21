"""Client for the bearer-authenticated in-cluster aiquota API."""

from dataclasses import dataclass, field

import httpx

from aiquota.models import AllQuotas

QUOTAS_PATH = "/v1/quotas"


@dataclass(frozen=True)
class QuotaAPIClient:
    """Fetch normalized quotas without exposing provider credentials locally."""

    url: str
    bearer_token: str | None = field(repr=False)
    timeout: float = 5.0

    async def fetch(self) -> AllQuotas:
        token = self.bearer_token
        if not token:
            raise ValueError("aiquota API bearer token is empty")

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.get(
                f"{self.url.rstrip('/')}{QUOTAS_PATH}", headers={"Authorization": f"Bearer {token}"}
            )
        response.raise_for_status()
        return AllQuotas.model_validate(response.json())
