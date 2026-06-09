"""Persist eval-run results to the `loom-gym` bucket on cluster S3.

Runs land at `s3://loom-gym/runs/<utc-timestamp>-<model>-<mode>.json` via the
public gateway (`s3.allegedly.works`). Writer credentials come from the
session environment (`LOOM_GYM_S3_*`, exported by `devinfra/secrets/web_env.sh`
from the claude-web-decryptable sops secret); the identity is scoped to this
one bucket by the gateway config.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from typing import Any

import boto3

logger = logging.getLogger(__name__)

BUCKET = "loom-gym"
DEFAULT_ENDPOINT = "https://s3.allegedly.works"


def results_client() -> Any:
    return boto3.client(
        "s3",
        endpoint_url=os.environ.get("AWS_ENDPOINT_URL", DEFAULT_ENDPOINT),
        aws_access_key_id=os.environ["LOOM_GYM_S3_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["LOOM_GYM_S3_SECRET_ACCESS_KEY"],
        region_name="us-east-1",
    )


def run_key(model_id: str, mode: str, now: datetime) -> str:
    return f"runs/{now:%Y%m%dT%H%M%SZ}-{model_id}-{mode}.json"


def upload_run(client: Any, payload: dict[str, Any], now: datetime) -> str:
    """Upload one run payload; returns the object key."""
    key = run_key(model_id=payload["model_id"], mode=payload["mode"], now=now)
    client.put_object(
        Bucket=BUCKET, Key=key, Body=json.dumps(payload, indent=2).encode(), ContentType="application/json"
    )
    logger.info("uploaded s3://%s/%s", BUCKET, key)
    return key
