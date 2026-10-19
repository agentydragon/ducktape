"""Exercise S3 allocation and retained-object integrity during master maintenance."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import uuid
from pathlib import Path
from typing import Any

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
from kubernetes import client, config

PREFIX = "migration-canary/seaweed-master-persistence/"


def digest(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def verify(s3: Any, state: dict[str, Any], payload: bytes) -> None:
    baseline = s3.get_object(Bucket=state["bucket"], Key=state["baseline_key"])["Body"].read()
    if digest(baseline) != state["baseline_sha256"]:
        raise ValueError("The retained baseline object changed during maintenance")
    # Larger than inline filer storage, so each check exercises chunk assignment.
    s3.put_object(Bucket=state["bucket"], Key=state["key"], Body=payload)
    actual = s3.get_object(Bucket=state["bucket"], Key=state["key"])["Body"].read()
    if actual != payload:
        raise ValueError("S3 canary read-back does not match the written payload")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("start", "check", "finish"))
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--secret-namespace", required=True)
    parser.add_argument("--secret-name", required=True)
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--baseline-key", required=True)
    args = parser.parse_args()
    config.load_kube_config()
    secret = client.CoreV1Api().read_namespaced_secret(
        args.secret_name, args.secret_namespace, _request_timeout=(10, 20)
    )
    s3 = boto3.client(
        "s3",
        endpoint_url=args.endpoint,
        aws_access_key_id=base64.b64decode(secret.data["accessKey"]).decode(),
        aws_secret_access_key=base64.b64decode(secret.data["secretKey"]).decode(),
        config=Config(
            connect_timeout=10,
            read_timeout=20,
            retries={"mode": "standard", "total_max_attempts": 2},
            s3={"addressing_style": "path"},
        ),
    )
    payload_path = args.state.with_suffix(".payload")
    if args.phase == "start":
        if args.state.exists() or payload_path.exists():
            raise ValueError("Refusing to overwrite an existing canary run")
        payload = os.urandom(256 * 1024)
        baseline = s3.get_object(Bucket=args.bucket, Key=args.baseline_key)["Body"].read()
        state = {
            "endpoint": args.endpoint,
            "bucket": args.bucket,
            "key": PREFIX + str(uuid.uuid4()),
            "baseline_key": args.baseline_key,
            "baseline_sha256": digest(baseline),
            "payload_sha256": digest(payload),
        }
        payload_path.write_bytes(payload)
        args.state.write_text(json.dumps(state, indent=2) + "\n")
    else:
        state = json.loads(args.state.read_text())
        payload = payload_path.read_bytes()
    if (
        state["endpoint"] != args.endpoint
        or state["bucket"] != args.bucket
        or state["baseline_key"] != args.baseline_key
        or not state["key"].startswith(PREFIX)
        or digest(payload) != state["payload_sha256"]
    ):
        raise ValueError("Canary ownership, target or payload does not match this run")
    if args.phase != "start":
        previous = s3.get_object(Bucket=state["bucket"], Key=state["key"])["Body"].read()
        if previous != payload:
            raise ValueError("The existing canary object changed during maintenance")
    verify(s3, state, payload)
    if args.phase == "finish":
        s3.delete_object(Bucket=state["bucket"], Key=state["key"])
        try:
            s3.head_object(Bucket=state["bucket"], Key=state["key"])
        except ClientError as error:
            if error.response["ResponseMetadata"]["HTTPStatusCode"] != 404:
                raise
        else:
            raise ValueError("Canary object remains readable after cleanup")
    print(
        json.dumps(
            {"phase": args.phase, "key": state["key"], "baseline_sha256": state["baseline_sha256"], "verified": True}
        )
    )


if __name__ == "__main__":
    main()
