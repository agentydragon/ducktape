from datetime import UTC, date, datetime

import httpx
import pytest

from x.habitify_mcp.types import Status


@pytest.mark.parametrize(
    ("target", "expected"),
    [
        (date(2025, 1, 15), datetime(2025, 1, 15, tzinfo=UTC)),
        ("2025-05-20T23:45:00-07:00", datetime.fromisoformat("2025-05-20T23:45:00-07:00")),
        (datetime.fromisoformat("2025-05-20T23:45:00+09:00"), datetime.fromisoformat("2025-05-20T23:45:00+09:00")),
    ],
)
async def test_status_date_matches_requested_instant(client, patch_client_method, target, expected):
    response = httpx.Response(
        200, request=httpx.Request("GET", "https://example.invalid"), json={"data": {"status": "completed"}}
    )
    with patch_client_method("get", return_value=response) as get:
        status = await client.check_habit_status("test-habit", target)
        assert get.call_args.kwargs["params"]["target_date"] == expected.isoformat()
        assert status.date == expected
    with patch_client_method("put", return_value=response) as put:
        status = await client.set_habit_status("test-habit", Status.COMPLETED, target)
        assert put.call_args.kwargs["json"]["target_date"] == expected.isoformat()
        assert status.date == expected
