"""Per-job virtual keys on the workers-LiteLLM.

The key IS the zone boundary at the LLM layer: its model allowlist pins the job
to its zone's models, its budget/TTL bound the blast radius, and the
workers-LiteLLM CNP makes a leaked key unusable outside zone namespaces.
"""

import httpx


class LiteLLMKeyClient:
    def __init__(self, base_url: str, master_key: str) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url, headers={"Authorization": f"Bearer {master_key}"}, timeout=30
        )

    async def mint(self, job_id: str, models: list[str], max_budget_usd: float, ttl: str) -> str:
        response = await self._client.post(
            "/key/generate",
            json={
                "key_alias": job_id,
                "models": models,
                "max_budget": max_budget_usd,
                "duration": ttl,
                "metadata": {"job_id": job_id},
            },
        )
        response.raise_for_status()
        key: str = response.json()["key"]
        return key

    async def revoke(self, job_id: str) -> None:
        response = await self._client.post("/key/delete", json={"key_aliases": [job_id]})
        # 404 = key already expired/deleted; revocation is idempotent.
        if response.status_code != 404:
            response.raise_for_status()

    async def aclose(self) -> None:
        await self._client.aclose()
