"""An in-memory API server for the proxy's tests: TokenReview, Pod reads, list/watch, status patches.

Objects are kept in wire shape, with a global `resourceVersion` counter the way the real server
stamps them, so the informer's list-then-watch-from-version protocol is exercised for real: a watch
replays every change after the version it names, then streams live ones until `timeoutSeconds`
passes or `close_watches` ends it (which is how a test forces a relist).
"""

from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from aiohttp import web
from more_itertools import one

from x.agentplane.egress.identity import POD_NAME_CLAIM, POD_UID_CLAIM
from x.agentplane.egress.resources import (
    BINDINGS_PLURAL,
    GROUP,
    POLICIES_PLURAL,
    SANDBOX_GROUP,
    SANDBOX_KIND,
    SANDBOX_VERSION,
    SANDBOXES_PLURAL,
    VERSION,
)

NAMESPACE = "agentplane-egress-test"
SANDBOX_NAMESPACE = "agentplane-egress-test-sandboxes"
CREDENTIALS_NAMESPACE = "agentplane-egress-test-credentials"
SECRETS_PLURAL = "secrets"

# Which namespace each kind is legitimately read from. All three differ, so a proxy that asked the
# wrong one is a failed assertion rather than a test that passes because they happen to be equal.
_NAMESPACE_OF = {
    SECRETS_PLURAL: CREDENTIALS_NAMESPACE,
    SANDBOXES_PLURAL: SANDBOX_NAMESPACE,
    POLICIES_PLURAL: NAMESPACE,
    BINDINGS_PLURAL: NAMESPACE,
}


@dataclass(frozen=True)
class TokenVerdict:
    """What TokenReview says about one token string."""

    username: str
    pod_name: str
    pod_uid: str
    audiences: tuple[str, ...]
    authenticated: bool = True


@dataclass(frozen=True)
class _Event:
    version: int
    plural: str
    type: str
    obj: dict[str, Any]


@dataclass
class FakeApiServer:
    tokens: dict[str, TokenVerdict] = field(default_factory=dict)
    pods: dict[str, dict[str, Any]] = field(default_factory=dict)
    objects: dict[str, dict[str, dict[str, Any]]] = field(
        default_factory=lambda: {POLICIES_PLURAL: {}, BINDINGS_PLURAL: {}, SANDBOXES_PLURAL: {}, SECRETS_PLURAL: {}}
    )
    status_patches: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    token_reviews: int = 0
    pod_reads: int = 0
    port: int = 0
    _version: int = 0
    _events: list[_Event] = field(default_factory=list)
    _watchers: set[asyncio.Queue[_Event | None]] = field(default_factory=set)

    def put(self, plural: str, obj: dict[str, Any]) -> None:
        """Create or replace; stamps the next resourceVersion (and a uid on create) and wakes watches."""
        name = obj["metadata"]["name"]
        existing = self.objects[plural].get(name)
        self._version += 1
        metadata = {
            "uid": existing["metadata"]["uid"] if existing else f"uid-{plural}-{name}",
            "generation": (existing["metadata"]["generation"] + 1) if existing else 1,
            **obj["metadata"],
            "resourceVersion": str(self._version),
        }
        stored = {**obj, "metadata": metadata}
        if existing is not None and "status" in existing and "status" not in obj:
            stored["status"] = existing["status"]
        self.objects[plural][name] = stored
        self._emit(_Event(self._version, plural, "MODIFIED" if existing else "ADDED", stored))

    def delete(self, plural: str, name: str) -> None:
        self._version += 1
        self._emit(_Event(self._version, plural, "DELETED", self.objects[plural].pop(name)))

    def _emit(self, event: _Event) -> None:
        self._events.append(event)
        for queue in self._watchers:
            queue.put_nowait(event)

    def close_watches(self) -> None:
        """End every watch stream open right now, as the server does at `timeoutSeconds`.

        Only the ones open right now: an informer between a kind's list and its next watch
        registration keeps the watch it is about to open, for that watch's full lifetime. A test
        that needs a particular kind's cycle to end has to re-arm this until it does.
        """
        for queue in self._watchers:
            queue.put_nowait(None)

    async def token_review(self, request: web.Request) -> web.Response:
        self.token_reviews += 1
        body = await request.json()
        verdict = self.tokens.get(body["spec"]["token"])
        if verdict is None or not verdict.authenticated:
            status: dict[str, Any] = {"authenticated": False, "error": "[invalid bearer token]"}
        else:
            status = {
                "authenticated": True,
                "audiences": [aud for aud in body["spec"].get("audiences", []) if aud in verdict.audiences],
                "user": {
                    "username": verdict.username,
                    "uid": f"uid-{verdict.username}",
                    "extra": {POD_NAME_CLAIM: [verdict.pod_name], POD_UID_CLAIM: [verdict.pod_uid]},
                },
            }
        # The server echoes the spec back, token included; the client model insists on it.
        return web.json_response(
            {"apiVersion": "authentication.k8s.io/v1", "kind": "TokenReview", "spec": body["spec"], "status": status}
        )

    async def get_pod(self, request: web.Request) -> web.Response:
        self.pod_reads += 1
        assert request.match_info["namespace"] == SANDBOX_NAMESPACE
        pod = self.pods.get(request.match_info["name"])
        if pod is None:
            return web.json_response({"kind": "Status", "code": 404, "reason": "NotFound"}, status=404)
        return web.json_response(pod)

    async def list_or_watch(self, request: web.Request) -> web.StreamResponse:
        plural = request.match_info["plural"]
        assert request.match_info["namespace"] == _NAMESPACE_OF[plural]
        # The client spells the flag `True`, which the real server parses like `true`.
        if request.query.get("watch", "").lower() == "true":
            return await self._watch(request, plural)
        return web.json_response(
            {
                "apiVersion": "v1",
                "kind": "List",
                "metadata": {"resourceVersion": str(self._version)},
                "items": list(self.objects[plural].values()),
            }
        )

    async def _watch(self, request: web.Request, plural: str) -> web.StreamResponse:
        since = int(request.query.get("resourceVersion", "0"))
        # The API server parses timeoutSeconds with strconv.ParseInt and answers 400 for anything
        # else, so a float reaches it as "300.0" and every watch fails. Refusing it here too keeps
        # the difference from hiding that.
        raw_timeout = request.query.get("timeoutSeconds", "300")
        try:
            timeout = int(raw_timeout)
        except ValueError:
            return web.json_response(
                {
                    "kind": "Status",
                    "status": "Failure",
                    "code": 400,
                    "reason": "BadRequest",
                    "message": f'strconv.ParseInt: parsing "{raw_timeout}": invalid syntax',
                },
                status=400,
            )
        deadline = asyncio.get_running_loop().time() + timeout
        response = web.StreamResponse(headers={"Content-Type": "application/json"})
        response.enable_chunked_encoding()
        await response.prepare(request)
        queue: asyncio.Queue[_Event | None] = asyncio.Queue()
        for missed in self._events:
            if missed.version > since:
                queue.put_nowait(missed)
        self._watchers.add(queue)
        try:
            while True:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    break
                try:
                    event = await asyncio.wait_for(queue.get(), remaining)
                except TimeoutError:
                    break
                if event is None:
                    break
                if event.plural != plural:
                    continue
                await response.write((json.dumps({"type": event.type, "object": event.obj}) + "\n").encode())
        finally:
            self._watchers.discard(queue)
        await response.write_eof()
        return response

    async def patch_status(self, request: web.Request) -> web.Response:
        assert request.match_info["namespace"] == NAMESPACE
        assert request.content_type == "application/merge-patch+json"
        plural, name = request.match_info["plural"], request.match_info["name"]
        patch = await request.json()
        self.status_patches.append((name, patch["status"]))
        current = self.objects[plural][name]
        self.put(plural, {**current, "status": patch["status"]})
        return web.json_response(self.objects[plural][name])


def sandbox(name: str) -> dict[str, Any]:
    return {
        "apiVersion": f"{SANDBOX_GROUP}/{SANDBOX_VERSION}",
        "kind": SANDBOX_KIND,
        "metadata": {"name": name},
        "spec": {},
    }


def pod_for(fake: FakeApiServer, sandbox_name: str, *, pod_uid: str, ip: str) -> dict[str, Any]:
    """The Sandbox's Pod, controlled by the stored Sandbox and carrying `pod_uid` and `ip`."""
    owner = fake.objects[SANDBOXES_PLURAL][sandbox_name]["metadata"]
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": sandbox_name,
            "namespace": SANDBOX_NAMESPACE,
            "uid": pod_uid,
            "ownerReferences": [
                {
                    "apiVersion": f"{SANDBOX_GROUP}/{SANDBOX_VERSION}",
                    "kind": SANDBOX_KIND,
                    "name": sandbox_name,
                    "uid": owner["uid"],
                    "controller": True,
                }
            ],
        },
        "status": {"podIP": ip},
    }


def secret(name: str, data: dict[str, str]) -> dict[str, Any]:
    return {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {"name": name},
        "data": {key: base64.b64encode(value.encode()).decode() for key, value in data.items()},
    }


def policy(name: str, rules: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "apiVersion": f"{GROUP}/{VERSION}",
        "kind": "EgressPolicy",
        "metadata": {"name": name},
        "spec": {"rules": rules},
    }


def binding(
    name: str,
    *,
    subjects: list[dict[str, Any]],
    policies: list[str],
    expires_at: str | None = None,
    labels: dict[str, str] | None = None,
) -> dict[str, Any]:
    spec: dict[str, Any] = {"subjects": subjects, "policies": policies}
    if expires_at is not None:
        spec["expiresAt"] = expires_at
    return {
        "apiVersion": f"{GROUP}/{VERSION}",
        "kind": "EgressBinding",
        "metadata": {"name": name, "labels": labels or {}},
        "spec": spec,
    }


@asynccontextmanager
async def fake_apiserver() -> AsyncIterator[FakeApiServer]:
    fake = FakeApiServer()
    app = web.Application()
    app.router.add_post("/apis/authentication.k8s.io/v1/tokenreviews", fake.token_review)
    app.router.add_get("/api/v1/namespaces/{namespace}/pods/{name}", fake.get_pod)
    app.router.add_get("/api/v1/namespaces/{namespace}/{plural}", fake.list_or_watch)
    app.router.add_get("/apis/{group}/{version}/namespaces/{namespace}/{plural}", fake.list_or_watch)
    app.router.add_patch("/apis/{group}/{version}/namespaces/{namespace}/{plural}/{name}/status", fake.patch_status)
    # handler_cancellation: a watch handler outliving its disconnected client would otherwise
    # stall cleanup for the shutdown timeout.
    runner = web.AppRunner(app, handler_cancellation=True)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    fake.port = one(runner.addresses)[1]
    try:
        yield fake
    finally:
        fake.close_watches()
        await runner.cleanup()
