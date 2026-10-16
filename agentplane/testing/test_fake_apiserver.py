"""Streaming initial lists expose current state, a completion bookmark, then live changes."""

import asyncio
import json

import pytest_bazel
from aiohttp import ClientSession

from agentplane.sandbox_resource import SANDBOX_API_VERSION, SANDBOXES_PLURAL
from agentplane.testing.fake_apiserver import SANDBOX_NAMESPACE, fake_apiserver, sandbox


async def test_streaming_initial_list_uses_snapshot_then_live_changes() -> None:
    async with fake_apiserver() as fake, ClientSession() as client, asyncio.timeout(5):
        fake.put(SANDBOXES_PLURAL, sandbox("deleted"))
        fake.delete(SANDBOXES_PLURAL, "deleted")
        fake.put(SANDBOXES_PLURAL, sandbox("running"))
        fake.put(SANDBOXES_PLURAL, sandbox("running"))
        current = fake.objects[SANDBOXES_PLURAL]["running"]
        url = f"http://127.0.0.1:{fake.port}/apis/{SANDBOX_API_VERSION}/namespaces/{SANDBOX_NAMESPACE}/sandboxes"
        async with client.get(
            url,
            params={
                "watch": "true",
                "sendInitialEvents": "true",
                "allowWatchBookmarks": "true",
                "resourceVersion": "",
                "resourceVersionMatch": "NotOlderThan",
            },
        ) as response:
            response.raise_for_status()
            assert json.loads(await response.content.readline()) == {"type": "ADDED", "object": current}
            bookmark = json.loads(await response.content.readline())
            assert bookmark["type"] == "BOOKMARK"
            assert bookmark["object"]["metadata"] == {
                "resourceVersion": current["metadata"]["resourceVersion"],
                "annotations": {"k8s.io/initial-events-end": "true"},
            }
            fake.delete(SANDBOXES_PLURAL, "running")
            assert json.loads(await response.content.readline()) == {"type": "DELETED", "object": current}


async def test_empty_streaming_initial_list_has_typed_bookmark() -> None:
    async with (
        fake_apiserver(namespace_of={"leases": SANDBOX_NAMESPACE}) as fake,
        ClientSession() as client,
        asyncio.timeout(5),
    ):
        url = f"http://127.0.0.1:{fake.port}/apis/coordination.k8s.io/v1/namespaces/{SANDBOX_NAMESPACE}/leases"
        async with client.get(url, params={"watch": "true", "sendInitialEvents": "true"}) as response:
            response.raise_for_status()
            assert json.loads(await response.content.readline()) == {
                "type": "BOOKMARK",
                "object": {
                    "apiVersion": "coordination.k8s.io/v1",
                    "kind": "Lease",
                    "metadata": {"resourceVersion": "0", "annotations": {"k8s.io/initial-events-end": "true"}},
                },
            }


if __name__ == "__main__":
    pytest_bazel.main()
