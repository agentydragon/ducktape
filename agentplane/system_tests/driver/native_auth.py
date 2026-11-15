"""Mint audience-scoped credentials bound to the native system-test fixture Pod."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

from agentplane.system_tests.driver.cluster import Cluster

NATIVE_CLIENT_NAME = "system-test-native-client"
NATIVE_CLIENT_LABEL = "app.kubernetes.io/name=system-test-native-client"


async def write_bound_fixture_token(cluster: Cluster, token_file: Path, *, audience: str) -> tuple[str, str]:
    """Write a Pod-bound token without returning or logging its secret; return Pod name and UID."""
    pods = json.loads(await cluster.kubectl("get", "pods", "-l", NATIVE_CLIENT_LABEL, "-o", "json"))
    ready = [
        pod
        for pod in pods["items"]
        if not pod["metadata"].get("deletionTimestamp")
        and pod.get("spec", {}).get("serviceAccountName") == NATIVE_CLIENT_NAME
        and any(
            condition.get("type") == "Ready" and condition.get("status") == "True"
            for condition in pod.get("status", {}).get("conditions", [])
        )
        and pod["metadata"].get("name")
        and pod["metadata"].get("uid")
    ]
    if len(ready) != 1:
        raise RuntimeError(f"expected one Ready {NATIVE_CLIENT_NAME} Pod, found {len(ready)}")
    pod = ready[0]["metadata"]
    name, uid = pod["name"], pod["uid"]
    token = await cluster.kubectl(
        "create",
        "token",
        NATIVE_CLIENT_NAME,
        f"--audience={audience}",
        "--duration=1800s",
        "--bound-object-kind=Pod",
        f"--bound-object-name={name}",
        f"--bound-object-uid={uid}",
    )

    def write_private() -> None:
        token_file.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(token_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            output = os.fdopen(descriptor, "w")
        except BaseException:
            os.close(descriptor)
            raise
        with output:
            os.fchmod(output.fileno(), 0o600)
            output.write(token)

    await asyncio.to_thread(write_private)
    return name, uid
