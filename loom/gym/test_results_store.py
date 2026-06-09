from __future__ import annotations

import json
from datetime import UTC, datetime

import boto3
import pytest_bazel
from botocore.stub import ANY, Stubber

from loom.gym.results_store import BUCKET, run_key, upload_run

PAYLOAD = {"model_id": "glm-4.5", "mode": "data-bundled", "results": []}
NOW = datetime(2026, 6, 9, 21, 30, 0, tzinfo=UTC)


def test_run_key_encodes_model_mode_and_time() -> None:
    assert run_key(model_id="glm-4.5", mode="bare", now=NOW) == "runs/20260609T213000Z-glm-4.5-bare.json"


def test_upload_puts_json_object() -> None:
    client = boto3.client(
        "s3",
        endpoint_url="https://example.test",
        aws_access_key_id="test-access",
        aws_secret_access_key="test-secret",
        region_name="us-east-1",
    )
    expected_key = run_key(model_id="glm-4.5", mode="data-bundled", now=NOW)
    stubber = Stubber(client)
    stubber.add_response(
        "put_object", {}, {"Bucket": BUCKET, "Key": expected_key, "Body": ANY, "ContentType": "application/json"}
    )
    with stubber:
        assert upload_run(client, PAYLOAD, now=NOW) == expected_key
    stubber.assert_no_pending_responses()
    assert json.dumps(PAYLOAD)  # payload itself must be JSON-serializable


if __name__ == "__main__":
    pytest_bazel.main()
