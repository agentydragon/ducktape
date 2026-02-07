#!/usr/bin/env python3
"""Rotate the rspcache client key used by Ember."""

from __future__ import annotations

import base64
import logging
import os
import sys
import time
from datetime import UTC, datetime

import httpx
from kubernetes import client, config
from kubernetes.client import ApiException

logger = logging.getLogger(__name__)

ADMIN_URL = os.environ.get("ADMIN_URL", "http://rspcache-admin.rspcache.svc.cluster.local:8100")
NAMESPACE = os.environ.get("NAMESPACE", "ember")
SECRET_NAME = os.environ.get("SECRET_NAME", "ember-rspcache-client")
UPSTREAM_ALIAS = os.environ.get("UPSTREAM_ALIAS", "default")
DEPLOYMENT_NAME = os.environ.get("DEPLOYMENT_NAME", "ember")


def admin_request(method: str, path: str, payload: dict | None = None) -> dict:
    """Call the rspcache admin API and return the JSON response."""

    url = f"{ADMIN_URL}{path}"
    with httpx.Client(timeout=30.0) as client_:
        response = client_.request(method, url, json=payload)
        response.raise_for_status()
        return response.json()


def decode_b64_optional(data: dict[str, str] | None, key: str) -> str | None:
    if not data:
        return None
    raw = data.get(key)
    if not raw:
        return None
    try:
        return base64.b64decode(raw).decode()
    except Exception as e:
        # Corrupt secret data is a critical error - log and reraise
        logger.error("Failed to decode base64 secret key '%s': %s", key, e, exc_info=True)
        raise


def rotate() -> None:
    config.load_incluster_config()
    core = client.CoreV1Api()
    apps = client.AppsV1Api()

    try:
        existing_secret = core.read_namespaced_secret(SECRET_NAME, NAMESPACE)
    except ApiException as exc:
        if exc.status != 404:
            raise
        existing_secret = None

    old_key_id = decode_b64_optional(existing_secret.data if existing_secret else None, "key_id")

    key_name = f"ember-{int(time.time())}"
    print(f"Minting new client key {key_name}")
    created = admin_request("POST", "/api/keys", {"name": key_name, "alias": UPSTREAM_ALIAS})
    token_value = created.get("token")
    record = created.get("record", {})
    new_key_id = record.get("id")
    token_prefix = record.get("token_prefix")

    if not token_value or not new_key_id:
        raise RuntimeError("Admin API response missing token or record id")

    if old_key_id:
        try:
            print(f"Revoking previous key {old_key_id}")
            admin_request("POST", f"/api/keys/{old_key_id}/revoke")
        except Exception as exc:  # pragma: no cover - warn and continue
            print(f"WARNING: failed to revoke old key {old_key_id}: {exc}", file=sys.stderr)

    created_ts = datetime.now(UTC).isoformat()
    data = {
        "openai_api_key": base64.b64encode(token_value.encode()).decode(),
        "token_prefix": base64.b64encode((token_prefix or "").encode()).decode(),
        "key_id": base64.b64encode(new_key_id.encode()).decode(),
        "created_at": base64.b64encode(created_ts.encode()).decode(),
    }
    metadata = client.V1ObjectMeta(name=SECRET_NAME, namespace=NAMESPACE)
    secret = client.V1Secret(metadata=metadata, type="Opaque", data=data)

    if existing_secret:
        secret.metadata.resource_version = existing_secret.metadata.resource_version
        core.replace_namespaced_secret(SECRET_NAME, NAMESPACE, secret)
        print("Updated existing secret")
    else:
        core.create_namespaced_secret(NAMESPACE, secret)
        print("Created new secret")

    patch = {"spec": {"template": {"metadata": {"annotations": {"rspcache/key-rotated-at": created_ts}}}}}
    apps.patch_namespaced_deployment(name=DEPLOYMENT_NAME, namespace=NAMESPACE, body=patch)
    print("Patched ember deployment to trigger rollout")


if __name__ == "__main__":
    rotate()
