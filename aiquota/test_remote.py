from datetime import UTC, datetime

import httpx
import pytest_bazel
import respx

from aiquota.models import AllQuotas
from aiquota.remote import QUOTAS_PATH, QuotaAPIClient

if __name__ == "__main__":
    pytest_bazel.main()


async def test_fetches_normalized_quotas_with_bearer_token() -> None:
    now = datetime.now(UTC).isoformat()
    body = {"providers": [], "fetched_at": now}

    with respx.mock() as mock:
        route = mock.get(f"https://aiquota.test{QUOTAS_PATH}").mock(return_value=httpx.Response(200, json=body))
        quotas = await QuotaAPIClient("https://aiquota.test", "api-bearer").fetch()

    assert isinstance(quotas, AllQuotas)
    assert route.calls.last.request.headers["Authorization"] == "Bearer api-bearer"
