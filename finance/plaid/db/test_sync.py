import json
from datetime import UTC, date, datetime

import pytest_bazel

from finance.plaid.db.sync import redact_payload


def test_redact_payload_returns_json_serializable_dates() -> None:
    payload = redact_payload(
        {
            "access_token": "secret",
            "start_date": date(2026, 5, 1),
            "nested": [{"captured_at": datetime(2026, 5, 31, 2, 7, tzinfo=UTC)}],
        }
    )

    assert payload == {
        "access_token": "<redacted>",
        "start_date": "2026-05-01",
        "nested": [{"captured_at": "2026-05-31T02:07:00+00:00"}],
    }
    json.dumps(payload)


if __name__ == "__main__":
    pytest_bazel.main()
