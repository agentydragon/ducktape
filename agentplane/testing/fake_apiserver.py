"""An in-memory API server for informer tests: TokenReview, Pod reads, list/watch, creates,
replacements and status patches.

Objects are kept in wire shape, with a global `resourceVersion` counter the way the real server
stamps them, so the informer's list-then-watch-from-version protocol is exercised for real: a watch
replays every change after the version it names, then streams live ones until `timeoutSeconds`
passes or `close_watches` ends it (which is how a test forces a relist). The kinds served and the
namespace each is legitimately read from default to the egress proxy's; another informer's tests
pass their own map.
"""

from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from aiohttp import web
from more_itertools import one

from agentplane.crd_group import GROUP, VERSION
from agentplane.egress.resources import CREDENTIALS_PLURAL
from agentplane.policy_sets import POLICY_BINDINGS_PLURAL, POLICY_SETS_PLURAL
from agentplane.sandbox_resource import SANDBOX_API_VERSION, SANDBOX_KIND, SANDBOXES_PLURAL
from agentplane.workload_auth.principal import POD_NAME_CLAIM, POD_UID_CLAIM

NAMESPACE = "agentplane-egress-test"
SANDBOX_NAMESPACE = "agentplane-egress-test-sandboxes"
CREDENTIALS_NAMESPACE = "agentplane-egress-test-credentials"
SECRETS_PLURAL = "secrets"

# Which namespace each kind is legitimately read from. All three differ, so a proxy that asked the
# wrong one is a failed assertion rather than a test that passes because they happen to be equal.
_NAMESPACE_OF = {
    SECRETS_PLURAL: CREDENTIALS_NAMESPACE,
    SANDBOXES_PLURAL: SANDBOX_NAMESPACE,
    CREDENTIALS_PLURAL: NAMESPACE,
    POLICY_SETS_PLURAL: NAMESPACE,
    POLICY_BINDINGS_PLURAL: NAMESPACE,
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


def _selected(selector: str, obj: dict[str, Any]) -> bool:
    """Whether `obj` matches a `key=value[,key=value]` label selector; the empty selector matches all."""
    labels = obj["metadata"].get("labels", {})
    return all(
        labels.get(key) == value for key, _, value in (item.partition("=") for item in selector.split(",")) if key
    )


@dataclass
class FakeApiServer:
    tokens: dict[str, TokenVerdict] = field(default_factory=dict)
    pods: dict[str, dict[str, Any]] = field(default_factory=dict)
    namespace_of: dict[str, str] = field(default_factory=lambda: dict(_NAMESPACE_OF))
    objects: dict[str, dict[str, dict[str, Any]]] = field(default_factory=dict)
    status_patches: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    token_reviews: int = 0
    pod_reads: int = 0
    port: int = 0
    _version: int = 0
    _events: list[_Event] = field(default_factory=list)
    _watchers: set[asyncio.Queue[_Event | None]] = field(default_factory=set)
    _changed: asyncio.Event = field(default_factory=asyncio.Event)

    def __post_init__(self) -> None:
        for plural in self.namespace_of:
            self.objects.setdefault(plural, {})

    def put(self, plural: str, obj: dict[str, Any]) -> None:
        """Create or replace; stamps the next resourceVersion (and a uid on create) and wakes watches."""
        name = obj["metadata"]["name"]
        existing = self.objects[plural].get(name)
        self._version += 1
        metadata = {
            "uid": existing["metadata"]["uid"] if existing else f"uid-{plural}-{name}",
            "generation": (existing["metadata"]["generation"] + 1) if existing else 1,
            "namespace": self.namespace_of[plural],
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
        self._changed.set()

    async def wait_for(self, predicate: Callable[[], bool]) -> None:
        """Until `predicate` holds over the stored objects, re-checked after every change."""
        while True:
            self._changed.clear()
            if predicate():
                return
            await self._changed.wait()

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

    async def list_pods(self, request: web.Request) -> web.Response:
        assert request.match_info["namespace"] == SANDBOX_NAMESPACE
        selector = request.query.get("labelSelector", "")
        return web.json_response(
            {
                "apiVersion": "v1",
                "kind": "PodList",
                "metadata": {"resourceVersion": str(self._version)},
                "items": [pod for pod in self.pods.values() if _selected(selector, pod)],
            }
        )

    watch_available: bool = True

    async def list_or_watch(self, request: web.Request) -> web.StreamResponse:
        if not self.watch_available:
            return web.Response(status=503)
        plural = request.match_info["plural"]
        assert request.match_info["namespace"] == self.namespace_of[plural]
        selector = request.query.get("labelSelector", "")
        # The client spells the flag `True`, which the real server parses like `true`.
        if request.query.get("watch", "").lower() == "true":
            return await self._watch(request, plural, selector)
        return web.json_response(
            {
                "apiVersion": "v1",
                "kind": "List",
                "metadata": {"resourceVersion": str(self._version)},
                "items": [obj for obj in self.objects[plural].values() if _selected(selector, obj)],
            }
        )

    async def _watch(self, request: web.Request, plural: str, selector: str) -> web.StreamResponse:
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
                # As the real server does under a selector: an object that stopped matching is
                # reported deleted, one that never matched is not reported at all.
                event_type = event.type if _selected(selector, event.obj) else "DELETED"
                await response.write((json.dumps({"type": event_type, "object": event.obj}) + "\n").encode())
        finally:
            self._watchers.discard(queue)
        await response.write_eof()
        return response

    async def get_object(self, request: web.Request) -> web.Response:
        plural, name = request.match_info["plural"], request.match_info["name"]
        assert request.match_info["namespace"] == self.namespace_of[plural]
        obj = self.objects[plural].get(name)
        if obj is None:
            return web.json_response({"kind": "Status", "code": 404, "reason": "NotFound"}, status=404)
        return web.json_response(obj)

    async def create_object(self, request: web.Request) -> web.Response:
        plural = request.match_info["plural"]
        assert request.match_info["namespace"] == self.namespace_of[plural]
        body = await request.json()
        if body["metadata"]["name"] in self.objects[plural]:
            return web.json_response({"kind": "Status", "code": 409, "reason": "AlreadyExists"}, status=409)
        self.put(plural, body)
        return web.json_response(self.objects[plural][body["metadata"]["name"]], status=201)

    async def replace_object(self, request: web.Request) -> web.Response:
        """PUT, with the `resourceVersion` precondition the real server applies when the body names one."""
        plural, name = request.match_info["plural"], request.match_info["name"]
        assert request.match_info["namespace"] == self.namespace_of[plural]
        body = await request.json()
        current = self.objects[plural].get(name)
        if current is None:
            return web.json_response({"kind": "Status", "code": 404, "reason": "NotFound"}, status=404)
        expected = body["metadata"].get("resourceVersion")
        if expected is not None and expected != current["metadata"]["resourceVersion"]:
            return web.json_response({"kind": "Status", "code": 409, "reason": "Conflict"}, status=409)
        self.put(plural, body)
        return web.json_response(self.objects[plural][name])

    async def patch_object(self, request: web.Request) -> web.Response:
        """A JSON merge patch, with the `resourceVersion` precondition the real server applies when
        the patch names one."""
        plural, name = request.match_info["plural"], request.match_info["name"]
        assert request.match_info["namespace"] == self.namespace_of[plural]
        assert request.content_type == "application/merge-patch+json"
        patch = await request.json()
        current = self.objects[plural].get(name)
        if current is None:
            return web.json_response({"kind": "Status", "code": 404, "reason": "NotFound"}, status=404)
        expected = patch.get("metadata", {}).get("resourceVersion")
        if expected is not None and expected != current["metadata"]["resourceVersion"]:
            return web.json_response({"kind": "Status", "code": 409, "reason": "Conflict"}, status=409)
        self.put(plural, _merge_patch(current, patch))
        return web.json_response(self.objects[plural][name])

    async def patch_status(self, request: web.Request) -> web.Response:
        plural, name = request.match_info["plural"], request.match_info["name"]
        assert request.match_info["namespace"] == self.namespace_of[plural]
        patch = await request.json()
        self.status_patches.append((name, patch["status"]))
        current = self.objects[plural][name]
        if request.content_type == "application/merge-patch+json":
            self.put(plural, {**current, "status": patch["status"]})
        else:
            # Server-side apply of conditions, a list keyed by `type`: the applied ones replace their
            # namesakes and the rest stay. A status write leaves `generation` alone, as on a real server.
            assert request.content_type == "application/apply-patch+yaml"
            assert request.query["fieldManager"]
            assert request.query["force"].lower() == "true"
            applied = {condition["type"]: condition for condition in patch["status"]["conditions"]}
            kept = [c for c in current.get("status", {}).get("conditions", []) if c["type"] not in applied]
            self._version += 1
            stored = {
                **current,
                "metadata": {**current["metadata"], "resourceVersion": str(self._version)},
                "status": {"conditions": [*kept, *applied.values()]},
            }
            self.objects[plural][name] = stored
            self._emit(_Event(self._version, plural, "MODIFIED", stored))
        return web.json_response(self.objects[plural][name])


def _merge_patch(target: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    """RFC 7396: objects merge recursively, null removes a key, anything else replaces."""
    merged = dict(target)
    for key, value in patch.items():
        if value is None:
            merged.pop(key, None)
        elif isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge_patch(merged[key], value)
        else:
            merged[key] = value
    return merged


def sandbox(name: str) -> dict[str, Any]:
    return {
        "apiVersion": SANDBOX_API_VERSION,
        "kind": SANDBOX_KIND,
        "metadata": {"name": name, "creationTimestamp": "2026-09-01T11:00:00Z"},
        "spec": {"template": "test-runner", "running": True},
    }


def sandbox_uid(name: str) -> str:
    """The UID this server stamps on a stored Sandbox, which a binding's subject has to name."""
    return f"uid-{SANDBOXES_PLURAL}-{name}"


def pod_for(fake: FakeApiServer, sandbox_name: str, *, pod_uid: str, ip: str) -> dict[str, Any]:
    """The Sandbox's Pod, controlled by the stored Sandbox and carrying `pod_uid` and `ip`, which the
    stored Sandbox reports as its incarnation as the Sandbox Controller would."""
    stored = fake.objects[SANDBOXES_PLURAL][sandbox_name]
    stored.setdefault("status", {})["incarnation"] = {"name": sandbox_name, "uid": pod_uid}
    owner = stored["metadata"]
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": sandbox_name,
            "namespace": SANDBOX_NAMESPACE,
            "uid": pod_uid,
            "ownerReferences": [
                {
                    "apiVersion": SANDBOX_API_VERSION,
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


def credential(
    name: str, *, secret_name: str, key: str, targets: list[dict[str, Any]], description: str = "a test credential"
) -> dict[str, Any]:
    return {
        "apiVersion": f"{GROUP}/{VERSION}",
        "kind": "EgressCredential",
        "metadata": {"name": name},
        "spec": {
            "source": {"secretRef": {"name": secret_name, "key": key}},
            "description": description,
            "targets": targets,
        },
    }


def authenticated_workload_credential(
    name: str, *, targets: list[dict[str, Any]], description: str = "the calling workload's authenticated bearer"
) -> dict[str, Any]:
    return {
        "apiVersion": f"{GROUP}/{VERSION}",
        "kind": "EgressCredential",
        "metadata": {"name": name},
        "spec": {"source": {"authenticatedWorkloadToken": {}}, "description": description, "targets": targets},
    }


def projected_workload_credential(
    name: str,
    *,
    audience: str,
    targets: list[dict[str, Any]],
    description: str = "the calling workload's identity for another audience",
) -> dict[str, Any]:
    return {
        "apiVersion": f"{GROUP}/{VERSION}",
        "kind": "EgressCredential",
        "metadata": {"name": name},
        "spec": {
            "source": {"projectedWorkloadToken": {"audience": audience}},
            "description": description,
            "targets": targets,
        },
    }


def policy_set(name: str, rules: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "apiVersion": f"{GROUP}/{VERSION}",
        "kind": "PolicySet",
        "metadata": {"name": name},
        "spec": {"rules": rules},
    }


def policy_binding(name: str, *, subject: dict[str, str], policy_sets: list[str]) -> dict[str, Any]:
    return {
        "apiVersion": f"{GROUP}/{VERSION}",
        "kind": "PolicyBinding",
        "metadata": {"name": name},
        "spec": {"subject": subject, "policySets": policy_sets},
    }


def egress_policy_set(name: str, rules: list[dict[str, Any]]) -> dict[str, Any]:
    return policy_set(name, [{"type": "egress", **rule} for rule in rules])


@asynccontextmanager
async def fake_apiserver(namespace_of: dict[str, str] | None = None) -> AsyncIterator[FakeApiServer]:
    fake = FakeApiServer() if namespace_of is None else FakeApiServer(namespace_of=namespace_of)
    app = web.Application()
    app.router.add_post("/apis/authentication.k8s.io/v1/tokenreviews", fake.token_review)
    app.router.add_get("/api/v1/namespaces/{namespace}/pods/{name}", fake.get_pod)
    app.router.add_get("/api/v1/namespaces/{namespace}/pods", fake.list_pods)
    app.router.add_get("/api/v1/namespaces/{namespace}/{plural}", fake.list_or_watch)
    app.router.add_get("/api/v1/namespaces/{namespace}/{plural}/{name}", fake.get_object)
    app.router.add_post("/api/v1/namespaces/{namespace}/{plural}", fake.create_object)
    app.router.add_put("/api/v1/namespaces/{namespace}/{plural}/{name}", fake.replace_object)
    app.router.add_get("/apis/{group}/{version}/namespaces/{namespace}/{plural}", fake.list_or_watch)
    app.router.add_get("/apis/{group}/{version}/namespaces/{namespace}/{plural}/{name}", fake.get_object)
    app.router.add_post("/apis/{group}/{version}/namespaces/{namespace}/{plural}", fake.create_object)
    app.router.add_put("/apis/{group}/{version}/namespaces/{namespace}/{plural}/{name}", fake.replace_object)
    app.router.add_patch("/apis/{group}/{version}/namespaces/{namespace}/{plural}/{name}", fake.patch_object)
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
