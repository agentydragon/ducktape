"""Signed App delivery, durable replay, provider matching, and access boundaries."""

import asyncio
import hashlib
import hmac
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from unittest.mock import create_autospec, patch
from uuid import UUID, uuid4

import httpx
import jwt
import pytest
import pytest_bazel
from alembic import command
from alembic.config import Config
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from pydantic import JsonValue, SecretStr, ValidationError
from sqlalchemy import ColumnElement, func, select, text, update
from sqlalchemy.engine import Connection
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from agentplane.notification_service.api import authenticated_caller, create_app
from agentplane.notification_service.database_migrate import RUNNER
from agentplane.notification_service.db import (
    GitHubDelivery,
    GitHubDeliverySubject,
    GitHubInstallation,
    GitHubRepository,
    GitHubRepositoryAccess,
    GitHubSubject,
    GitHubSubjectRevision,
    Inbox,
    Subscription,
)
from agentplane.notification_service.github_state import (
    AccessKey,
    GitHubState,
    RefreshDeferredError,
    RefreshLease,
    SubjectKey,
)
from agentplane.notification_service.models import (
    DestinationRef,
    EventIdentity,
    SourceFailureKind,
    Subscribe,
    SubscriptionUpdate,
)
from agentplane.notification_service.service import Service
from agentplane.notification_service.settings import CONFIG_FILE_ENV, GitHubSettings, NoticeDebounceSettings, Settings
from agentplane.notification_service.sources.actions import Actions
from agentplane.notification_service.sources.github import GitHub, IssuePayload, RefPayload, correlation
from agentplane.notification_service.sources.github_client import (
    GitHubAccessError,
    GitHubClient,
    GitHubRetryError,
    GitHubSourceChangedError,
    GitHubUnavailableError,
    Repository,
    rate_limit_delay,
)
from agentplane.notification_service.sources.github_models import (
    BranchSubject,
    CommitSubject,
    EventFilter,
    EventName,
    GitHubSource,
    IssueSubject,
    PullRequestSubject,
    Subject,
)
from agentplane.notification_service.store import ConflictError, NotFoundError, Store
from agentplane.sandbox_service.client import Runner, SandboxServiceClient
from agentplane.subjects import ServiceAccountRef
from agentplane.workload_auth.principal import WorkloadPrincipal, WorkloadPrincipalResolver

HEAD = "a" * 40
NEXT = "b" * 40
SECRET = b"test-webhook-secret!"  # 20 characters: accepted on startup and used by the signed-delivery tests.
PRINCIPAL = WorkloadPrincipal("test", "owner", "system:serviceaccount:test:owner", "pod", "uid")
SOURCE = GitHubSource(
    provider="github", repository="owner/repo", subject=PullRequestSubject(kind="pull_request", number=7)
)


def subscription(source: GitHubSource = SOURCE, key: str = "follow") -> Subscribe:
    return Subscribe(
        destination_ref=DestinationRef(namespace="test", name="sandbox", uid="uid"),
        session_id="session",
        idempotency_key=key,
        source=source,
    )


def comment(number: int = 7) -> dict[str, JsonValue]:
    return {
        "action": "created",
        "installation": {"id": 11},
        "repository": {"id": 100, "full_name": "owner/repo"},
        "issue": {"number": number, "pull_request": {"url": "https://api.github.com/repos/owner/repo/pulls/7"}},
        "comment": {"body": "kept verbatim"},
    }


def pr(sha: str) -> dict[str, JsonValue]:
    return {
        "action": "synchronize",
        "installation": {"id": 11},
        "repository": {"id": 100, "full_name": "owner/repo"},
        "pull_request": {"number": 7, "head": {"sha": sha, "repo": {"id": 100, "full_name": "owner/repo"}}},
    }


def check(sha: str = HEAD) -> dict[str, JsonValue]:
    return {
        "action": "completed",
        "installation": {"id": 11},
        "repository": {"id": 100, "full_name": "owner/repo"},
        "check_run": {"head_sha": sha, "pull_requests": []},
    }


def signed(payload: dict[str, JsonValue], event: str, delivery: UUID | None = None) -> tuple[bytes, dict[str, str]]:
    raw = json.dumps(payload).encode()
    return raw, {
        "X-GitHub-Event": event,
        "X-GitHub-Delivery": str(delivery or uuid4()),
        "X-Hub-Signature-256": "sha256=" + hmac.new(SECRET, raw, hashlib.sha256).hexdigest(),
    }


@dataclass
class Upstream:
    public_key: bytes
    head: str | None = HEAD
    api_version: str = "2022-11-28"
    revoked: bool = False
    limited: bool = False
    fork: bool = False
    requests: list[str] = field(default_factory=list)
    responses: dict[str, httpx.Response] = field(default_factory=dict)

    def handle(self, request: httpx.Request) -> httpx.Response:
        assert request.headers["Accept"] == "application/vnd.github+json"
        assert request.headers["X-GitHub-Api-Version"] == self.api_version
        path = request.url.path
        self.requests.append(path)
        if path.endswith(("/installation", "/access_tokens")):
            claims = jwt.decode(request.headers["Authorization"][7:], self.public_key, algorithms=["RS256"])
            assert claims["iss"] == "42"
        if path in self.responses:
            return self.responses[path]
        if self.limited:
            return httpx.Response(403, headers={"x-ratelimit-remaining": "0", "retry-after": "120"})
        if self.revoked:
            return httpx.Response(404)
        fork = "/fork/repo" in path or path == "/repositories/200"
        if path.endswith("/installation"):
            return httpx.Response(200, json={"id": 22 if fork else 11})
        if path.endswith("/access_tokens"):
            permissions = json.loads(request.content)["permissions"]
            assert all(value == "read" for value in permissions.values())
            return httpx.Response(
                201,
                json={
                    "token": "fixture-installation-token",
                    "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
                },
            )
        assert request.headers["Authorization"] == "Bearer fixture-installation-token"
        if path in {"/repos/owner/repo", "/repos/fork/repo", "/repositories/100", "/repositories/200"}:
            return httpx.Response(
                200, json={"id": 200 if fork else 100, "full_name": "fork/repo" if fork else "owner/repo"}
            )
        if "/pulls/" in path:
            return httpx.Response(
                200,
                json={
                    "number": 7,
                    "head": {
                        "sha": self.head,
                        "repo": {
                            "id": 200 if self.fork else 100,
                            "full_name": "fork/repo" if self.fork else "owner/repo",
                        },
                    },
                },
            )
        if "/issues/" in path:
            return httpx.Response(200, json={"number": int(path.rsplit("/", 1)[1])})
        if "/git/ref/" in path:
            return (
                httpx.Response(404) if self.head is None else httpx.Response(200, json={"object": {"sha": self.head}})
            )
        if "/commits/" in path:
            return httpx.Response(200, json={"sha": path.rsplit("/", 1)[1]})
        raise AssertionError(path)


@pytest.fixture
async def provider() -> AsyncIterator[tuple[GitHub, Upstream]]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()
    upstream = Upstream(
        key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    )
    settings = GitHubSettings(app_id=42, private_key=SecretStr(private), webhook_secret=SecretStr(SECRET.decode()))
    async with httpx.AsyncClient(
        base_url="https://api.github.test", transport=httpx.MockTransport(upstream.handle)
    ) as http:
        github = GitHub(GitHubClient(http, settings))
        github.client.start()
        yield github, upstream


async def test_github_configured_transport_and_version(provider: tuple[GitHub, Upstream]) -> None:
    github, upstream = provider
    assert str(github.settings.api_url) == "https://api.github.com/"
    assert github.settings.request_timeout_s == 5
    assert github.settings.api_version == date(2022, 11, 28)
    configured = GitHubSettings.model_validate(
        github.settings.model_dump()
        | {"api_url": "http://github.test/api/v3", "request_timeout_s": 2.5, "api_version": "2026-01-01"}
    )
    async with GitHubClient.open(configured) as client:
        request = client.http.build_request("GET", "/repos/owner/repo")
        assert str(request.url) == "http://github.test/api/v3/repos/owner/repo"
        assert client.http.timeout == httpx.Timeout(2.5)
        assert not client.http.follow_redirects
    assert client.http.is_closed
    upstream.api_version = "2026-01-01"
    overridden = GitHub(GitHubClient(github.client.http, configured))
    overridden.client.start()
    # The mock upstream checks the configured header on App JWT requests, token creation,
    # and installation-token repository/subject requests, not just one helper's output.
    await overridden.context(SOURCE)
    assert "/repos/owner/repo/installation" in upstream.requests
    assert any(path.endswith("/access_tokens") for path in upstream.requests)
    assert "/repos/owner/repo/pulls/7" in upstream.requests


async def test_github_client_shares_and_invalidates_installation_tokens(provider: tuple[GitHub, Upstream]) -> None:
    github, upstream = provider
    client = github.client
    await asyncio.gather(
        client.repository("owner/repo", 11),
        client.pull_request("owner/repo", 7, 11),
        client.issue("owner/repo", 8, 11),
        client.branch("owner/repo", "devel", 11),
        client.commit("owner/repo", HEAD, 11),
    )
    token_path = "/app/installations/11/access_tokens"
    assert upstream.requests.count(token_path) == 1
    client.tokens[11].expires_at = datetime.now(UTC)
    await client.repository_by_id(100, 11)
    assert upstream.requests.count(token_path) == 2
    upstream.responses["/repos/owner/repo"] = httpx.Response(401)
    with pytest.raises(GitHubAccessError):
        await client.repository("owner/repo", 11)
    assert not client.tokens
    del upstream.responses["/repos/owner/repo"]
    await client.repository("owner/repo", 11)
    assert upstream.requests.count(token_path) == 3


async def test_github_client_never_follows_redirects(provider: tuple[GitHub, Upstream]) -> None:
    github, upstream = provider
    # Even an injected HTTP client with redirects enabled must not forward the credentials.
    github.client.http.follow_redirects = True
    upstream.responses["/repos/owner/repo"] = httpx.Response(
        302, headers={"location": "https://untrusted.test/capture"}
    )
    with pytest.raises(httpx.HTTPStatusError) as error:
        await github.client.repository("owner/repo", 11)
    assert error.value.response.status_code == 302
    assert "/capture" not in upstream.requests


async def ingest(github: GitHub, store: Store, payload: dict[str, JsonValue], event: str = "issue_comment") -> None:
    raw, headers = signed(payload, event)
    await github.ingest(store, event, UUID(headers["X-GitHub-Delivery"]), headers["X-Hub-Signature-256"], raw)


async def test_signed_http_durable_acceptance_and_disabled_provider(
    store: Store, provider: tuple[GitHub, Upstream]
) -> None:
    github, _ = provider
    service = Service(
        store,
        create_autospec(Actions),
        create_autospec(SandboxServiceClient),
        github,
        notice_debounce=NoticeDebounceSettings(),
        stale_confirmation_s=30,
    )
    app = create_app(service, create_autospec(WorkloadPrincipalResolver))
    app.dependency_overrides[authenticated_caller] = lambda: PRINCIPAL
    raw, headers = signed(comment(), "issue_comment")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://notifications") as client:
        assert "github" in (await client.get("/v1/sources")).json()
        assert (
            await client.post("/v1/webhooks/github", content=raw, headers=headers | {"X-Hub-Signature-256": "bad"})
        ).status_code == 401
        assert (
            await client.post(
                "/v1/webhooks/github", content=b"x" * (github.settings.max_body_bytes + 1), headers=headers
            )
        ).status_code == 413
        with patch.object(Store, "ingest_github", side_effect=ConnectionError("database unavailable")):
            assert (await client.post("/v1/webhooks/github", content=raw, headers=headers)).status_code == 503
        for _ in range(github.settings.webhook_concurrency):
            await github.ingress_slots.acquire()
        try:
            assert (await client.post("/v1/webhooks/github", content=raw, headers=headers)).status_code == 503
        finally:
            for _ in range(github.settings.webhook_concurrency):
                github.ingress_slots.release()
        first = await client.post("/v1/webhooks/github", content=raw, headers=headers)
        assert first.status_code == 202
        assert first.json() == {"accepted": True, "duplicate": False}
        assert (await client.post("/v1/webhooks/github", content=raw, headers=headers)).json()["duplicate"]
        changed, changed_headers = signed(comment(8), "issue_comment", UUID(headers["X-GitHub-Delivery"]))
        assert (await client.post("/v1/webhooks/github", content=changed, headers=changed_headers)).status_code == 409
        malformed, malformed_headers = signed({"installation": {"id": 11}}, "issue_comment")
        assert (
            await client.post("/v1/webhooks/github", content=malformed, headers=malformed_headers)
        ).status_code == 400
        for event in ["check_suite", "unknown_event"]:
            # Event selection comes from the header; a check_run body must not select its own model.
            mismatched, mismatched_headers = signed(check(), event)
            assert (
                await client.post("/v1/webhooks/github", content=mismatched, headers=mismatched_headers)
            ).status_code == 400
        service.github = None
        assert "github" not in (await client.get("/v1/sources")).json()
        assert (await client.post("/v1/webhooks/github", content=raw, headers=headers)).status_code == 404
    async with store.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(GitHubDelivery)) == 1
        delivery = await session.scalar(select(GitHubDelivery))
        assert delivery is not None
        assert delivery.payload == comment()


@pytest.mark.parametrize(
    ("case", "reason", "status"),
    [
        ("delivery_id", "invalid_delivery_id", 400),
        ("signature", "invalid_signature", 401),
        ("event", "unsupported_event", 400),
        ("repository", "missing_repository", 400),
        ("schema", "payload_validation", 400),
        ("json", "payload_validation", 400),
        ("value_error", "invalid_delivery", 400),
    ],
)
async def test_webhook_rejection_diagnostics_are_bounded_and_redacted(
    store: Store,
    provider: tuple[GitHub, Upstream],
    caplog: pytest.LogCaptureFixture,
    case: str,
    reason: str,
    status: int,
) -> None:
    github, _ = provider
    service = create_autospec(Service, instance=True)
    service.github = github
    service.store = store
    app = create_app(service, create_autospec(WorkloadPrincipalResolver))
    private = "private-payload-sentinel"
    payload = comment()
    payload["comment"] = {"body": private}
    event = "issue_comment"
    if case == "event":
        event = "untrusted\n" + "x" * 200
    elif case == "repository":
        del payload["repository"]
    elif case == "schema":
        payload["installation"] = {"id": private}
        payload["repository"] = {"id": private, "full_name": private}
        payload["issue"] = {"number": private, "pull_request": private}
        payload["action"] = private * 10
    raw, headers = signed(payload, event)
    delivery_id = headers["X-GitHub-Delivery"]
    if case == "delivery_id":
        headers["X-GitHub-Delivery"] = private
    elif case == "signature":
        headers["X-Hub-Signature-256"] = private
    elif case == "json":
        raw = ("{invalid " + private).encode()
        headers["X-Hub-Signature-256"] = "sha256=" + hmac.new(SECRET, raw, hashlib.sha256).hexdigest()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://notifications") as client:
        if case == "value_error":
            with patch.object(Store, "ingest_github", side_effect=ValueError(private)):
                response = await client.post("/v1/webhooks/github", content=raw, headers=headers)
        else:
            response = await client.post("/v1/webhooks/github", content=raw, headers=headers)
    assert response.status_code == status
    assert response.json() == {"detail": "invalid GitHub signature" if status == 401 else "invalid GitHub delivery"}
    records = [record for record in caplog.records if record.message.startswith("GitHub webhook rejected: ")]
    assert len(records) == 1
    record = records[0]
    assert record.exc_info is None
    assert "\n" not in record.message
    assert len(record.message) < 2000
    diagnostic = json.loads(record.message.removeprefix("GitHub webhook rejected: "))
    assert diagnostic["reason"] == reason
    assert diagnostic["delivery_id"] == (None if case == "delivery_id" else delivery_id)
    assert diagnostic["event"] == event[:64]
    assert diagnostic["event_truncated"] == (len(event) > 64)
    if case in {"schema", "json"}:
        assert diagnostic["error_count"] > 0
        assert 0 < len(diagnostic["validation"]) <= 8
        assert all(set(item) == {"field", "type"} for item in diagnostic["validation"])
    if case == "schema":
        assert {"field": "installation", "type": "int_parsing"} in diagnostic["validation"]
    if case == "json":
        assert diagnostic["validation"] == [{"field": "<payload>", "type": "json_invalid"}]
    assert private not in caplog.text
    assert SECRET.decode() not in caplog.text
    assert headers["X-Hub-Signature-256"] not in caplog.text
    async with store.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(GitHubDelivery)) == 0


async def test_redelivery_after_restart_replays_one_committed_receipt(
    store: Store, engine: AsyncEngine, provider: tuple[GitHub, Upstream]
) -> None:
    github, _ = provider
    sub = await store.subscribe(PRINCIPAL, subscription(), (await github.context(SOURCE)).binding)
    raw, headers = signed(comment(), "issue_comment")
    delivery_id = UUID(headers["X-GitHub-Delivery"])
    signature = headers["X-Hub-Signature-256"]
    assert await github.ingest(store, "issue_comment", delivery_id, signature, raw)

    # Reconstruct both the provider and store before the inbox worker processes the receipt.
    recovered = Store(engine)
    restarted = GitHub(GitHubClient(github.client.http, github.settings))
    assert not await restarted.ingest(recovered, "issue_comment", delivery_id, signature, raw)
    claim = await recovered.claim()
    assert claim is not None
    source = await recovered.source(claim)
    assert source is not None
    await restarted.reconcile(recovered, claim, source, SOURCE)
    page = await recovered.read(PRINCIPAL.account, sub.inbox_id, 0, 128)
    assert len(page.entries) == 1
    assert page.entries[0].payload == comment()
    assert not await restarted.ingest(recovered, "issue_comment", delivery_id, signature, raw)
    await restarted.reconcile(recovered, claim, source, SOURCE)
    assert (await recovered.read(PRINCIPAL.account, sub.inbox_id, 0, 128)).entries == page.entries
    async with recovered.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(GitHubDelivery)) == 1


@pytest.mark.parametrize(
    "values", [{"actions_after_sequence": 0}, {"github_start_position": None}, {"github_app_id": None}]
)
async def test_github_subscription_state_is_source_specific(
    store: Store, provider: tuple[GitHub, Upstream], values: dict[str, int | None]
) -> None:
    github, _ = provider
    sub = await store.subscribe(PRINCIPAL, subscription(), (await github.context(SOURCE)).binding)
    with pytest.raises(IntegrityError, match="subscription_source_state"):
        async with store.sessions.begin() as session:
            await session.execute(update(Subscription).where(Subscription.id == sub.id).values(**values))


@pytest.mark.parametrize("name", ["owner/repo", "o/" + "r" * 198])
def test_repository_names_accept_valid_names(name: str) -> None:
    assert Repository(id=1, full_name=name).full_name == name
    assert GitHubSource(provider="github", repository=name, subject=SOURCE.subject).repository == name


@pytest.mark.parametrize("name", ["repo", "owner/repo/extra", "owner/repo name", "o/" + "r" * 199])
def test_repository_names_reject_invalid_names(name: str) -> None:
    with pytest.raises(ValidationError):
        Repository(id=1, full_name=name)
    with pytest.raises(ValidationError):
        GitHubSource(provider="github", repository=name, subject=SOURCE.subject)


async def test_replay_boundary_overlapping_matches_and_revocation(
    store: Store, engine: AsyncEngine, provider: tuple[GitHub, Upstream]
) -> None:
    github, upstream = provider
    await ingest(github, store, comment())  # Before subscription boundary: not a historical replay.
    binding = (await github.context(SOURCE)).binding
    first = await store.subscribe(PRINCIPAL, subscription(), binding)
    second = await store.subscribe(PRINCIPAL, subscription(key="overlap"), binding)
    await ingest(github, store, comment(8))
    await ingest(github, store, comment())
    claim = await store.claim()
    assert claim is not None
    # A fresh provider/Store can finish fanout after HTTP acceptance; no in-memory queue is needed.
    recovered = Store(engine)
    for identity in [first.id, second.id]:
        async with recovered.sessions() as session:
            row = await session.get(Subscription, identity)
        assert row is not None
        await github.reconcile(recovered, claim, row, SOURCE)
    page = await store.read(PRINCIPAL.account, first.inbox_id, 0, 128)
    assert len(page.entries) == 1
    assert set(page.entries[0].subscriptions) == {first.id, second.id}
    assert page.entries[0].payload == comment()
    assert page.inbox.acknowledged == 0
    notice = await store.notice(claim)
    assert notice is not None
    assert notice.through_cursor == 1
    page = await store.read(PRINCIPAL.account, first.inbox_id, 0, 128)
    with pytest.raises(NotFoundError):
        await store.read(ServiceAccountRef(namespace="test", name="other"), first.inbox_id, 0, 128)
    await ingest(github, store, comment())
    async with store.sessions() as session:
        row = await session.get(Subscription, first.id)
    assert row is not None
    upstream.revoked = True
    await ingest(github, store, {"installation": {"id": 11}, "action": "suspend"}, "installation")
    with pytest.raises(RefreshDeferredError):
        await github.reconcile(store, claim, row, SOURCE)
    after = await store.read(PRINCIPAL.account, first.inbox_id, 0, 128)
    assert after.entries == page.entries
    assert after.inbox == page.inbox
    upstream.revoked = False
    upstream.limited = True
    with pytest.raises(GitHubRetryError) as error:
        await github.context(SOURCE)
    assert error.value.retry_seconds == 120


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        ({}, 60),
        ({"retry-after": "120"}, 120),
        ({"retry-after": "7200"}, 7200),
        ({"retry-after": "1"}, 60),
        ({"retry-after": "invalid"}, 60),
        ({"retry-after": "-1"}, 60),
        ({"retry-after": "Thu, 01 Jan 1970 01:00:00 GMT"}, 2600),
        ({"retry-after": "Thu, 01 Jan 1970 00:00:00 GMT"}, 60),
        ({"x-ratelimit-remaining": "0", "x-ratelimit-reset": "3600"}, 2600),
        ({"x-ratelimit-remaining": "0", "x-ratelimit-reset": "999"}, 60),
        ({"x-ratelimit-remaining": "0", "x-ratelimit-reset": "invalid"}, 60),
        ({"x-ratelimit-remaining": "1", "x-ratelimit-reset": "3600"}, 60),
        ({"retry-after": "120", "x-ratelimit-remaining": "0", "x-ratelimit-reset": "3600"}, 2600),
        ({"retry-after": "7200", "x-ratelimit-remaining": "0", "x-ratelimit-reset": "3600"}, 7200),
    ],
)
def test_rate_limit_deadlines(headers: dict[str, str], expected: int) -> None:
    assert rate_limit_delay(httpx.Headers(headers), 1000.25) == expected


@pytest.mark.parametrize("status", [403, 429])
async def test_primary_limit_uses_reset_without_retry_after(provider: tuple[GitHub, Upstream], status: int) -> None:
    github, upstream = provider
    upstream.responses["/repos/owner/repo/installation"] = httpx.Response(
        status, headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": "3600"}, text="private upstream body"
    )
    headers = github.client.app_headers()
    with (
        patch("agentplane.notification_service.sources.github_client.time.time", return_value=1000.25),
        pytest.raises(GitHubRetryError, match=f"HTTP {status}") as failure,
    ):
        await github.client.request("GET", "/repos/owner/repo/installation", headers)
    assert failure.value.retry_seconds == 2600
    assert "private upstream body" not in str(failure.value)


async def test_rate_limit_retry_survives_ingress_and_restart(
    store: Store, engine: AsyncEngine, provider: tuple[GitHub, Upstream], caplog: pytest.LogCaptureFixture
) -> None:
    github, upstream = provider
    sub = await store.subscribe(PRINCIPAL, subscription(), (await github.context(SOURCE)).binding)
    await ingest(github, store, comment())
    upstream.limited = True
    service = Service(
        store,
        create_autospec(Actions),
        create_autospec(SandboxServiceClient),
        github,
        notice_debounce=NoticeDebounceSettings(quiet_seconds=3600, max_wait_seconds=3600),
        stale_confirmation_s=30,
    )
    runner = create_autospec(Runner)
    with patch.object(Service, "runner", return_value=runner):
        before = datetime.now(UTC)
        assert await service.step()
        async with store.sessions() as session:
            source = await session.get(Subscription, sub.id)
            assert source is not None
            retry_at = source.next_attempt
            assert retry_at is not None
            assert before + timedelta(seconds=120) <= retry_at <= datetime.now(UTC) + timedelta(seconds=120)
        view = await store.subscription(PRINCIPAL.account, sub.id)
        assert view.error is None
        assert view.retry_at is None
        assert view.github is not None
        access = view.github.access[0]
        assert access.error == "GitHub rate limited (HTTP 403); retry in 120s"
        assert access.retry_at == retry_at
        assert "GitHub shared refresh failed" in caplog.text
        assert "retry_seconds=120" in caplog.text
        page = await store.read(PRINCIPAL.account, sub.inbox_id, 0, 128)
        assert not page.entries
        assert page.inbox.last_cursor == 0
        assert page.notice is None
        assert access.error_kind == SourceFailureKind.RATE_LIMITED

        requests = len(upstream.requests)
        await ingest(github, store, comment())
        assert await service.step()
        assert len(upstream.requests) == requests
        waiting = await store.subscription(PRINCIPAL.account, sub.id)
        assert waiting.github is not None
        assert waiting.github.access[0].retry_at == retry_at
        assert await store.get_next_work_at() == retry_at

        # Advance the durable deadline, then recover with no process-local provider/store state
        # and no further webhook to wake the source.
        async with store.sessions.begin() as session:
            await session.execute(update(GitHubRepositoryAccess).values(next_attempt=datetime.now(UTC)))
            await session.execute(
                update(Subscription).where(Subscription.id == sub.id).values(next_attempt=datetime.now(UTC))
            )
            await session.execute(update(Inbox).where(Inbox.id == sub.inbox_id).values(next_attempt=datetime.now(UTC)))
        upstream.limited = False
        recovered = Store(engine)
        restarted = Service(
            recovered,
            service.actions,
            service.sandboxes,
            GitHub(GitHubClient(github.client.http, github.settings)),
            notice_debounce=service.notice_debounce,
            stale_confirmation_s=30,
        )
        assert await restarted.step()
    view = await recovered.subscription(PRINCIPAL.account, sub.id)
    assert view.error is None
    assert view.retry_at is None
    page = await recovered.read(PRINCIPAL.account, sub.inbox_id, 0, 128)
    assert view.error_kind is None
    assert view.last_success_at is not None
    assert len(page.entries) == 2
    assert all(entry.payload == comment() for entry in page.entries)
    assert page.inbox.acknowledged == 0


@pytest.mark.parametrize(
    ("path", "status"),
    [
        ("/repos/fork/repo/installation", 401),
        ("/repos/fork/repo/installation", 403),
        ("/app/installations/22/access_tokens", 401),
        ("/app/installations/22/access_tokens", 403),
        ("/app/installations/22/access_tokens", 404),
        ("/repos/fork/repo", 404),
    ],
)
async def test_fork_access_failures_are_not_treated_as_uninstalled(
    provider: tuple[GitHub, Upstream], path: str, status: int
) -> None:
    github, upstream = provider
    upstream.fork = True
    upstream.responses[path] = httpx.Response(status)
    with pytest.raises(GitHubUnavailableError, match=f"HTTP {status}"):
        await github.context(SOURCE)


async def test_uninstalled_fork_is_optional_but_suspension_is_an_error(provider: tuple[GitHub, Upstream]) -> None:
    github, upstream = provider
    upstream.fork = True
    upstream.responses["/repos/fork/repo/installation"] = httpx.Response(404)
    context = await github.context(SOURCE)
    assert context.installations == {100: 11}
    assert context.heads == {HEAD}
    upstream.responses["/repos/fork/repo/installation"] = httpx.Response(
        200, json={"id": 22, "suspended_at": datetime.now(UTC).isoformat()}
    )
    with pytest.raises(GitHubUnavailableError, match="suspended"):
        await github.context(SOURCE)


async def test_filter_order_and_duplicates_do_not_change_subscription_identity(
    store: Store, provider: tuple[GitHub, Upstream]
) -> None:
    github, _ = provider
    source = GitHubSource(
        provider="github",
        repository=SOURCE.repository,
        subject=SOURCE.subject,
        events={
            EventFilter(event=EventName.ISSUE_COMMENT, actions={"created", "edited"}),
            EventFilter(event=EventName.CHECK_RUN, actions={"completed"}),
        },
    )
    body = subscription(source)
    binding = (await github.context(source)).binding
    original = await store.subscribe(PRINCIPAL, body, binding)
    # Alter ordering and duplicates at the JSON request boundary, not in typed sets.
    wire = json.loads(body.model_dump_json())
    filters = wire["source"]["events"]
    filters.reverse()
    for event_filter in filters:
        event_filter["actions"].reverse()
        event_filter["actions"].append(event_filter["actions"][0])
    filters.append(filters[0])
    retry = Subscribe.model_validate(wire)
    assert retry.model_dump(mode="json") == body.model_dump(mode="json")
    assert (await store.subscribe(PRINCIPAL, retry, binding)).id == original.id
    # Existing noncanonical JSON is interpreted using the same set semantics.
    async with store.sessions.begin() as session:
        row = await session.get(Subscription, original.id)
        assert row is not None
        row.creation = wire
    assert (await store.subscribe(PRINCIPAL, body, binding)).id == original.id
    filters[0]["actions"] = ["deleted"]
    with pytest.raises(ConflictError):
        await store.subscribe(PRINCIPAL, Subscribe.model_validate(wire), binding)


@pytest.mark.parametrize("kind", ["pull_request", "branch"])
@pytest.mark.parametrize("ci_first", [True, False])
async def test_late_correlation_survives_restart_and_does_not_block_other_events(
    store: Store, engine: AsyncEngine, provider: tuple[GitHub, Upstream], kind: str, ci_first: bool
) -> None:
    github, upstream = provider
    source = (
        SOURCE
        if kind == "pull_request"
        else SOURCE.model_copy(update={"subject": BranchSubject(kind="branch", name="devel")})
    )
    base: dict[str, JsonValue] = {"installation": {"id": 11}, "repository": {"id": 100, "full_name": "owner/repo"}}
    association = (
        base | {"action": "synchronize", "pull_request": {"number": 7, "head": {"sha": NEXT}}}
        if kind == "pull_request"
        else base | {"ref": "refs/heads/devel", "after": NEXT}
    )
    event = "pull_request" if kind == "pull_request" else "push"
    # A later association must not pull receipts from before subscription creation into the inbox.
    await ingest(github, store, check(NEXT), "check_run")
    sub = await store.subscribe(PRINCIPAL, subscription(source), (await github.context(source)).binding)
    if ci_first:
        await ingest(github, store, check(NEXT), "check_run")
    else:
        await ingest(github, store, association, event)
    # No prefix cap: unrelated receipts must not hide a definite match farther into the journal.
    for _ in range(130):
        await ingest(github, store, check("c" * 40), "check_run")
    await ingest(github, store, check(HEAD), "check_run")
    claim = await store.claim()
    assert claim is not None
    row = await store.source(claim)
    assert row is not None
    boundary = row.github_start_position
    await github.reconcile(store, claim, row, source)
    page = await store.read(PRINCIPAL.account, sub.inbox_id, 0, 128)
    assert [entry.payload for entry in page.entries] == ([check(HEAD)] if ci_first else [association, check(HEAD)])
    async with store.sessions() as session:
        assert await session.scalar(select(Subscription.next_attempt).where(Subscription.id == sub.id)) is not None
    await store.release(claim, None)
    # The next association/receipt arrives long after the former grace window, with fresh objects.
    async with store.sessions.begin() as session:
        await session.execute(update(GitHubDelivery).values(received_at=datetime.now(UTC) - timedelta(days=2)))
    recovered = Store(engine)
    restarted = GitHub(GitHubClient(github.client.http, github.settings))
    if ci_first:
        await ingest(restarted, recovered, association, event)
    else:
        await ingest(restarted, recovered, check(NEXT), "check_run")
    # Upstream has already moved on: only durable webhook evidence can associate NEXT.
    upstream.head = "d" * 40
    claim = await recovered.claim()
    assert claim is not None
    row = await recovered.source(claim)
    assert row is not None
    await restarted.reconcile(recovered, claim, row, source)
    page = await recovered.read(PRINCIPAL.account, sub.inbox_id, 0, 128)
    assert len(page.entries) == 3
    assert sum(entry.payload == check(NEXT) for entry in page.entries) == 1
    async with recovered.sessions() as session:
        row = await session.get(Subscription, sub.id)
    assert row is not None
    assert row.github_start_position == boundary
    assert row.next_attempt is not None
    await restarted.reconcile(recovered, claim, row, source)
    assert (await recovered.read(PRINCIPAL.account, sub.inbox_id, 0, 128)).entries == page.entries


async def test_matching_pages_and_old_head_associations_are_not_capped(
    store: Store, provider: tuple[GitHub, Upstream]
) -> None:
    github, _ = provider
    source = SOURCE.model_copy(update={"events": {EventFilter(event=EventName.CHECK_RUN, actions={"completed"})}})
    # Association history predates this subscription and is longer than a delivery page.
    for index in range(130):
        await ingest(
            github,
            store,
            {
                "installation": {"id": 11},
                "repository": {"id": 100, "full_name": "owner/repo"},
                "action": "synchronize",
                "pull_request": {"number": 7, "head": {"sha": f"{index:040x}"}},
            },
            "pull_request",
        )
    sub = await store.subscribe(PRINCIPAL, subscription(source), (await github.context(source)).binding)
    await ingest(github, store, check(f"{1:040x}") | {"action": "created"}, "check_run")
    for _ in range(130):
        await ingest(github, store, check(f"{1:040x}"), "check_run")
    claim = await store.claim()
    assert claim is not None
    row = await store.source(claim)
    assert row is not None
    await github.reconcile(store, claim, row, source)
    assert (await store.read(PRINCIPAL.account, sub.inbox_id, 0, 128)).inbox.last_cursor == 128
    row = await store.source(claim)
    assert row is not None  # Full matching page schedules immediate continuation.
    await github.reconcile(store, claim, row, source)
    page = await store.read(PRINCIPAL.account, sub.inbox_id, 128, 128)
    assert len(page.entries) == 2
    assert page.inbox.last_cursor == 130
    assert await store.source(claim) is None


async def test_retained_fork_receipt_requires_current_installation_access(
    store: Store, provider: tuple[GitHub, Upstream]
) -> None:
    github, upstream = provider
    upstream.fork = True
    sub = await store.subscribe(PRINCIPAL, subscription(), (await github.context(SOURCE)).binding)
    payload = check(NEXT) | {"repository": {"id": 200, "full_name": "fork/repo"}, "installation": {"id": 22}}
    await ingest(github, store, payload, "check_run")
    # Same SHA in an unrelated installation must never be admitted.
    await ingest(github, store, payload | {"installation": {"id": 33}}, "check_run")
    upstream.head = NEXT
    upstream.responses["/repos/fork/repo/installation"] = httpx.Response(404)
    claim = await store.claim()
    assert claim is not None
    row = await store.source(claim)
    assert row is not None
    await github.reconcile(store, claim, row, SOURCE)
    assert not (await store.read(PRINCIPAL.account, sub.inbox_id, 0, 128)).entries
    del upstream.responses["/repos/fork/repo/installation"]
    # Missing installation coverage is repaired on the bounded shared subject deadline.
    async with store.sessions.begin() as session:
        await session.execute(update(GitHubSubject).values(last_success_at=datetime.now(UTC) - timedelta(hours=1)))
    await ingest(github, store, comment())
    row = await store.source(claim)
    assert row is not None
    await github.reconcile(store, claim, row, SOURCE)
    page = await store.read(PRINCIPAL.account, sub.inbox_id, 0, 128)
    assert [entry.payload for entry in page.entries] == [payload, comment()]


@pytest.mark.parametrize("kind", ["branch", "commit"])
async def test_branch_activity_and_fixed_commit(store: Store, provider: tuple[GitHub, Upstream], kind: str) -> None:
    github, upstream = provider
    subject = BranchSubject(kind="branch", name="devel") if kind == "branch" else CommitSubject(kind="commit", sha=HEAD)
    source = SOURCE.model_copy(update={"subject": subject})
    sub = await store.subscribe(PRINCIPAL, subscription(source), (await github.context(source)).binding)
    base: dict[str, JsonValue] = {"installation": {"id": 11}, "repository": {"id": 100, "full_name": "owner/repo"}}
    await ingest(github, store, base | {"ref": "refs/heads/devel", "after": HEAD}, "push")
    upstream.head = NEXT
    await ingest(github, store, check(HEAD), "check_run")
    await ingest(github, store, base | {"ref": "devel", "ref_type": "branch"}, "delete")
    if kind == "branch":
        upstream.head = None
    else:
        # A fixed-SHA mismatch cannot become a match later.
        await ingest(github, store, check(NEXT), "check_run")
    claim = await store.claim()
    assert claim is not None
    async with store.sessions() as session:
        row = await session.get(Subscription, sub.id)
    assert row is not None
    await github.reconcile(store, claim, row, source)
    assert len((await store.read(PRINCIPAL.account, sub.inbox_id, 0, 128)).entries) == (3 if kind == "branch" else 1)
    if kind == "commit":
        async with store.sessions() as session:
            assert await session.scalar(select(Subscription.next_attempt).where(Subscription.id == sub.id)) is not None


@pytest.mark.parametrize(
    "event",
    [EventName.CHECK_RUN, EventName.CHECK_SUITE, EventName.STATUS, EventName.WORKFLOW_RUN, EventName.WORKFLOW_JOB],
)
async def test_native_ci_references_supply_durable_associations(
    store: Store, provider: tuple[GitHub, Upstream], event: EventName
) -> None:
    github, _ = provider
    source = (
        SOURCE
        if event in {EventName.CHECK_RUN, EventName.CHECK_SUITE}
        else SOURCE.model_copy(update={"subject": BranchSubject(kind="branch", name="devel")})
    )
    explicit = source.model_copy(update={"events": {EventFilter(event=event)}})
    binding = (await github.context(source)).binding
    default_sub = await store.subscribe(PRINCIPAL, subscription(source), binding)
    explicit_sub = await store.subscribe(PRINCIPAL, subscription(explicit, key="explicit"), binding)
    payload: dict[str, JsonValue] = {"installation": {"id": 11}, "repository": {"id": 100, "full_name": "owner/repo"}}
    match event:
        case EventName.CHECK_RUN | EventName.CHECK_SUITE:
            payload |= {"action": "completed", event: {"head_sha": NEXT, "pull_requests": [{"number": 7}]}}
        case EventName.STATUS:
            payload |= {"sha": NEXT, "branches": [{"name": "devel"}]}
        case EventName.WORKFLOW_RUN | EventName.WORKFLOW_JOB:
            payload |= {"action": "completed", event: {"head_sha": NEXT, "head_branch": "devel"}}
    # The empty-reference check arrives before its association, and upstream still reports HEAD.
    await ingest(github, store, check(NEXT), "check_run")
    await ingest(github, store, payload, event)
    claim = await store.claim()
    assert claim is not None
    for sub, spec in [(default_sub, source), (explicit_sub, explicit)]:
        async with store.sessions() as session:
            row = await session.get(Subscription, sub.id)
        assert row is not None
        await github.reconcile(store, claim, row, spec)
    page = await store.read(PRINCIPAL.account, default_sub.inbox_id, 0, 128)
    assert len(page.entries) == 2
    first, second = page.entries
    assert first.payload == check(NEXT)
    assert second.payload == payload
    assert (default_sub.id in second.subscriptions) == (event in {EventName.CHECK_RUN, EventName.STATUS})
    assert explicit_sub.id in second.subscriptions


@pytest.mark.parametrize("event", ["issues", "workflow_job"])
async def test_new_events_signed_http(store: Store, provider: tuple[GitHub, Upstream], event: str) -> None:
    github, _ = provider
    service = create_autospec(Service, instance=True)
    service.github = github
    service.store = store
    app = create_app(service, create_autospec(WorkloadPrincipalResolver))
    payload: dict[str, JsonValue] = {
        "installation": {"id": 11},
        "repository": {"id": 100, "full_name": "owner/repo"},
        "action": "opened" if event == "issues" else "completed",
    }
    payload |= {"issue": {"number": 7}} if event == "issues" else {"workflow_job": {"head_sha": HEAD}}
    raw, headers = signed(payload, event)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://notifications") as client:
        rejected = await client.post(
            "/v1/webhooks/github", content=raw, headers=headers | {"X-Hub-Signature-256": "bad"}
        )
        assert rejected.status_code == 401
        accepted = await client.post("/v1/webhooks/github", content=raw, headers=headers)
        assert accepted.status_code == 202
        assert accepted.json() == {"accepted": True, "duplicate": False}
        assert (await client.post("/v1/webhooks/github", content=raw, headers=headers)).json()["duplicate"]
        malformed, malformed_headers = signed({"installation": {"id": 11}}, event)
        assert (
            await client.post("/v1/webhooks/github", content=malformed, headers=malformed_headers)
        ).status_code == 400
    async with store.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(GitHubDelivery)) == 1


async def test_issue_lifecycle_comments_and_pr_separation(store: Store, provider: tuple[GitHub, Upstream]) -> None:
    github, _ = provider
    issue_source = GitHubSource(
        provider="github", repository="owner/repo", subject=IssueSubject(kind="issue", number=7)
    )
    specs = [
        issue_source,
        issue_source.model_copy(update={"events": {EventFilter(event=EventName.ISSUES, actions={"closed"})}}),
        SOURCE,
    ]
    subscriptions = [
        await store.subscribe(PRINCIPAL, subscription(spec, key=f"source-{i}"), (await github.context(spec)).binding)
        for i, spec in enumerate(specs)
    ]
    # GitHub's issue_comment envelope distinguishes issue and PR comments by the PR marker.
    ordinary = comment() | {"issue": {"number": 7}}
    payloads = [ordinary | {"action": "opened"}, ordinary | {"action": "closed"}, ordinary, comment()]
    for payload, event in zip(payloads, ["issues", "issues", "issue_comment", "issue_comment"], strict=True):
        await ingest(github, store, payload, event)
    await ingest(github, store, ordinary | {"issue": {"number": 8}}, "issues")
    claim = await store.claim()
    assert claim is not None
    for sub, spec in zip(subscriptions, specs, strict=True):
        async with store.sessions() as session:
            row = await session.get(Subscription, sub.id)
        assert row is not None
        await github.reconcile(store, claim, row, spec)
    page = await store.read(PRINCIPAL.account, subscriptions[0].inbox_id, 0, 128)
    assert [entry.payload for entry in page.entries] == payloads
    assert [set(entry.subscriptions) for entry in page.entries] == [
        {subscriptions[0].id},
        {subscriptions[0].id, subscriptions[1].id},
        {subscriptions[0].id},
        {subscriptions[2].id},
    ]


async def test_issue_subject_rejects_pr_and_missing_issue(provider: tuple[GitHub, Upstream]) -> None:
    github, upstream = provider
    source = GitHubSource(provider="github", repository="owner/repo", subject=IssueSubject(kind="issue", number=7))
    upstream.responses["/repos/owner/repo/issues/7"] = httpx.Response(200, json={"number": 7, "pull_request": {}})
    with pytest.raises(GitHubSourceChangedError):
        await github.context(source)
    upstream.responses["/repos/owner/repo/issues/7"] = httpx.Response(404)
    with pytest.raises(GitHubAccessError):
        await github.context(source)


@pytest.mark.parametrize(
    "subject",
    [
        PullRequestSubject(kind="pull_request", number=7),
        BranchSubject(kind="branch", name="devel"),
        CommitSubject(kind="commit", sha=HEAD),
    ],
)
async def test_workflow_job_sha_matching_and_action_filter(
    store: Store, provider: tuple[GitHub, Upstream], subject: Subject
) -> None:
    github, _ = provider
    source = GitHubSource(
        provider="github",
        repository="owner/repo",
        subject=subject,
        events={EventFilter(event=EventName.WORKFLOW_JOB, actions={"completed"})},
    )
    sub = await store.subscribe(PRINCIPAL, subscription(source), (await github.context(source)).binding)
    payload: dict[str, JsonValue] = {
        "installation": {"id": 11},
        "repository": {"id": 100, "full_name": "owner/repo"},
        "action": "completed",
        "workflow_job": {"head_sha": HEAD, "head_branch": None, "id": 123, "run_id": 456},
    }
    await ingest(github, store, payload | {"action": "in_progress"}, "workflow_job")
    await ingest(github, store, payload | {"workflow_job": {"head_sha": NEXT}}, "workflow_job")
    raw, headers = signed(payload, "workflow_job")
    assert await github.ingest(
        store, "workflow_job", UUID(headers["X-GitHub-Delivery"]), headers["X-Hub-Signature-256"], raw
    )
    assert not await github.ingest(
        store, "workflow_job", UUID(headers["X-GitHub-Delivery"]), headers["X-Hub-Signature-256"], raw
    )
    claim = await store.claim()
    assert claim is not None
    async with store.sessions() as session:
        row = await session.get(Subscription, sub.id)
    assert row is not None
    await github.reconcile(store, claim, row, source)
    page = await store.read(PRINCIPAL.account, sub.inbox_id, 0, 128)
    assert [entry.payload for entry in page.entries] == [payload]


def test_issue_and_ci_filter_vocabulary() -> None:
    issue = IssueSubject(kind="issue", number=7)
    with pytest.raises(ValidationError, match="event is not supported"):
        GitHubSource(
            provider="github",
            repository="owner/repo",
            subject=issue,
            events={EventFilter(event=EventName.WORKFLOW_JOB)},
        )
    with pytest.raises(ValidationError, match="event is not supported"):
        GitHubSource(
            provider="github",
            repository="owner/repo",
            subject=SOURCE.subject,
            events={EventFilter(event=EventName.ISSUES)},
        )
    assert EventName.WORKFLOW_JOB not in {event.event for event in SOURCE.filters}


async def test_cancellation_fences_accepted_github_work(store: Store, provider: tuple[GitHub, Upstream]) -> None:
    github, _ = provider
    sub = await store.subscribe(PRINCIPAL, subscription(), (await github.context(SOURCE)).binding)
    await ingest(github, store, comment())
    claim = await store.claim()
    assert claim is not None
    async with store.sessions() as session:
        row = await session.get(Subscription, sub.id)
    assert row is not None
    await store.change(PRINCIPAL.account, sub.id, None)
    await github.reconcile(store, claim, row, SOURCE)
    assert not (await store.read(PRINCIPAL.account, sub.inbox_id, 0, 128)).entries
    assert await store.source(claim) is None


async def test_webhook_wakes_idle_source_and_fences_concurrent_ingress(
    store: Store, provider: tuple[GitHub, Upstream]
) -> None:
    github, _ = provider
    sub = await store.subscribe(PRINCIPAL, subscription(), (await github.context(SOURCE)).binding)
    claim = await store.claim()
    assert claim is not None
    row = await store.source(claim)
    assert row is not None
    await github.reconcile(store, claim, row, SOURCE)
    await store.release(claim, None)
    assert await store.get_next_work_at() is not None  # Bounded repair even without further webhooks.
    await store.change(PRINCIPAL.account, sub.id, SubscriptionUpdate(version=sub.version, lifetime_days=7))
    claim = await store.claim()
    assert claim is not None
    row = await store.source(claim)
    assert row is not None  # Renewal also schedules an idle source, without waiting for a webhook.
    await github.reconcile(store, claim, row, SOURCE)
    await store.release(claim, None)
    assert await store.get_next_work_at() is not None
    async with store.wakeups.listener.listen():
        with store.wakeups.subscribe() as changed:
            await ingest(github, store, comment())
            async with asyncio.timeout(10):
                await changed.wait()
        claim = await store.claim()
        assert claim is not None
        source = await store.source(claim)
        assert source is not None
        # Simulate another receipt committed after the worker took its source snapshot.
        await ingest(github, store, comment())
        fence = await github.refresh_access(store, AccessKey(42, 11, 100))
        key = SubjectKey(100, "pull_request", "7")
        async with store.sessions() as session:
            subject = await key.load(session)
        await store.record_github(
            claim,
            source,
            [],
            more=False,
            fences=[fence],
            subject_key=key,
            subject_generation=subject.generation,
            repair_at=datetime.now(UTC) + timedelta(seconds=600),
        )
        await store.release(claim, None)
        claim = await store.claim()
        assert claim is not None
        source = await store.source(claim)
        assert source is not None
        await github.reconcile(store, claim, source, SOURCE)
        assert len((await store.read(PRINCIPAL.account, sub.inbox_id, 0, 128)).entries) == 2
        async with store.sessions() as session:
            source = await session.get(Subscription, sub.id)
            assert source is not None
            assert source.next_attempt is not None


async def test_github_data_prevents_lossy_downgrade(
    store: Store, engine: AsyncEngine, provider: tuple[GitHub, Upstream]
) -> None:
    github, _ = provider
    await ingest(github, store, comment())

    def downgrade(connection: Connection) -> None:
        config = Config()
        config.set_main_option("script_location", str(RUNNER.migrations_dir))
        config.attributes["connection"] = connection
        with pytest.raises(RuntimeError, match="refusing data loss"):
            command.downgrade(config, "0004_source_union")

    async with engine.begin() as connection:
        await connection.run_sync(downgrade)
    async with store.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(GitHubDelivery)) == 1


def test_yaml_settings_and_secret_separation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = tmp_path / "settings.yaml"
    config.write_text("""namespace: test
actions:
  url: http://actions
  token_file: /tokens/actions
sandbox_service:
  target: sandboxes:8080
  token_file: /tokens/sandboxes
github: null
""")
    monkeypatch.setenv(CONFIG_FILE_ENV, str(config))
    settings = Settings(database_url="postgresql://unused", _cli_parse_args=False)
    assert str(settings.actions.url) == "http://actions/"
    assert settings.actions.token_file == Path("/tokens/actions")
    assert settings.sandbox_service.target == "sandboxes:8080"
    assert settings.sandbox_service.token_file == Path("/tokens/sandboxes")
    assert settings.github is None
    config.write_text(config.read_text().replace("github: null\n", ""))
    assert Settings(database_url="postgresql://unused", _cli_parse_args=False).github is None
    monkeypatch.setenv("AGENTPLANE_NOTIFICATIONS_SANDBOX_SERVICE__TARGET", "overridden-sandboxes:8080")
    monkeypatch.setenv("AGENTPLANE_NOTIFICATIONS_ACTIONS__URL", "http://overridden-actions")
    settings = Settings(database_url="postgresql://unused", _cli_parse_args=False)
    assert str(settings.actions.url) == "http://overridden-actions/"
    assert settings.sandbox_service.target == "overridden-sandboxes:8080"
    assert settings.sandbox_service.token_file == Path("/tokens/sandboxes")
    # A present mapping enables GitHub and must be complete; secrets overlay public YAML.
    with config.open("a") as file:
        file.write("github:\n  app_id: 42\n")
    with pytest.raises(ValidationError, match="Field required"):
        Settings(database_url="postgresql://unused", _cli_parse_args=False)
    private = "fixture-private-key\nmultiline"
    secret = "test-signing-key"  # 16-character minimum.
    monkeypatch.setenv("AGENTPLANE_NOTIFICATIONS_GITHUB__PRIVATE_KEY", private)
    monkeypatch.setenv("AGENTPLANE_NOTIFICATIONS_GITHUB__WEBHOOK_SECRET", secret)
    settings = Settings(database_url="postgresql://unused", _cli_parse_args=False)
    assert settings.github is not None
    assert settings.github.app_id == 42
    assert settings.github.private_key.get_secret_value() == private
    assert settings.github.webhook_secret.get_secret_value() == secret
    assert private not in repr(settings)
    assert secret not in settings.model_dump_json()
    # Nested environment fields contribute presence even when YAML says null.
    config.write_text(config.read_text().replace("github:\n  app_id: 42\n", "github: null\n"))
    monkeypatch.setenv("AGENTPLANE_NOTIFICATIONS_GITHUB__APP_ID", "42")
    assert Settings(database_url="postgresql://unused", _cli_parse_args=False).github is not None
    monkeypatch.setenv("AGENTPLANE_NOTIFICATIONS_GITHUB__WEBHOOK_SECRET", "test-signing-ke")
    with pytest.raises(ValidationError) as failure:
        Settings(database_url="postgresql://unused", _cli_parse_args=False)
    assert "test-signing-ke" not in str(failure.value)
    config.unlink()
    with pytest.raises(ValueError, match="regular file"):
        Settings(database_url="postgresql://unused", _cli_parse_args=False)


async def test_subscriptions_share_normalized_github_entities(store: Store, provider: tuple[GitHub, Upstream]) -> None:
    github, _ = provider
    binding = (await github.context(SOURCE)).binding
    first = await store.subscribe(PRINCIPAL, subscription(), binding)
    second = await store.subscribe(PRINCIPAL, subscription(key="overlap"), binding)
    other = SOURCE.model_copy(update={"subject": PullRequestSubject(kind="pull_request", number=8)})
    await store.subscribe(PRINCIPAL, subscription(other, key="another-pr"), binding)
    assert first.id != second.id
    async with store.sessions() as session:
        for entity in (GitHubInstallation, GitHubRepository, GitHubRepositoryAccess):
            assert await session.scalar(select(func.count()).select_from(entity)) == 1
        assert await session.scalar(select(func.count()).select_from(GitHubSubject)) == 2
        assert await session.scalar(select(func.count()).select_from(Subscription)) == 3
        row = await session.get(Subscription, first.id)
        assert row is not None
        assert row.github_binding == binding.model_dump(mode="json")
    # A subscription cannot claim a subject or installation/repository grant that does not exist.
    with pytest.raises(IntegrityError):
        async with store.sessions.begin() as session:
            await session.execute(
                update(Subscription).where(Subscription.id == first.id).values(github_subject_key="999")
            )
    with pytest.raises(IntegrityError):
        async with store.sessions.begin() as session:
            await session.execute(
                update(Subscription).where(Subscription.id == first.id).values(github_installation_id=999)
            )


async def test_shared_refresh_requests_survive_restart(
    store: Store, engine: AsyncEngine, provider: tuple[GitHub, Upstream]
) -> None:
    github, upstream = provider
    binding = (await github.context(SOURCE)).binding
    first = await store.subscribe(PRINCIPAL, subscription(), binding)
    second = await store.subscribe(PRINCIPAL, subscription(key="overlap"), binding)
    another = SOURCE.model_copy(update={"subject": PullRequestSubject(kind="pull_request", number=8)})
    third = await store.subscribe(PRINCIPAL, subscription(another, key="another"), binding)
    upstream.requests.clear()
    claim = await store.claim()
    assert claim is not None
    for sub, spec in [(first, SOURCE), (second, SOURCE), (third, another)]:
        async with store.sessions() as session:
            row = await session.get(Subscription, sub.id)
        assert row is not None
        await github.reconcile(store, claim, row, spec)
    assert upstream.requests.count("/repos/owner/repo/installation") == 1
    assert upstream.requests.count("/repositories/100") == 1
    assert upstream.requests.count("/repos/owner/repo/pulls/7") == 1
    assert upstream.requests.count("/repos/owner/repo/pulls/8") == 1
    requests = list(upstream.requests)
    recovered = Store(engine)
    restarted = GitHub(GitHubClient(github.client.http, github.settings))
    await ingest(restarted, recovered, comment())
    for sub in [first, second]:
        async with recovered.sessions() as session:
            row = await session.get(Subscription, sub.id)
        assert row is not None
        await restarted.reconcile(recovered, claim, row, SOURCE)
    assert upstream.requests == requests
    page = await recovered.read(PRINCIPAL.account, first.inbox_id, 0, 128)
    assert len(page.entries) == 1
    assert set(page.entries[0].subscriptions) == {first.id, second.id}


async def test_shared_refresh_lease_takeover_and_failure_episode(
    store: Store, provider: tuple[GitHub, Upstream]
) -> None:
    github, _ = provider
    sub = await store.subscribe(PRINCIPAL, subscription(), (await github.context(SOURCE)).binding)
    state = GitHubState(store.sessions, 600)
    key = AccessKey(42, 11, 100)
    leases = await asyncio.gather(state.acquire(key), state.acquire(key), return_exceptions=True)
    winners = [lease for lease in leases if isinstance(lease, RefreshLease)]
    assert len(winners) == 1
    assert sum(isinstance(lease, RefreshDeferredError) for lease in leases) == 1
    old = winners[0]
    async with store.sessions.begin() as session:
        await session.execute(
            update(GitHubRepositoryAccess).values(claim_until=datetime.now(UTC) - timedelta(seconds=1))
        )
    lease = await state.acquire(key)
    assert lease is not None
    with pytest.raises(RefreshDeferredError):
        await state.succeed(old)
    with pytest.raises(RefreshDeferredError):
        await state.fail(old, SourceFailureKind.UNAVAILABLE, "stale failure", 60)
    until = await state.fail(lease, SourceFailureKind.RATE_LIMITED, "limited", 120)
    view = await store.subscription(PRINCIPAL.account, sub.id)
    assert view.github is not None
    initial = view.github.access[0]
    assert initial.retry_at == until
    assert not initial.currently_valid
    with pytest.raises(RefreshDeferredError) as deferred:
        await state.acquire(key)
    assert deferred.value.until == until
    async with store.sessions.begin() as session:
        await session.execute(update(GitHubRepositoryAccess).values(next_attempt=datetime.now(UTC)))
    lease = await state.acquire(key)
    assert lease is not None
    await state.fail(lease, SourceFailureKind.RATE_LIMITED, "still limited", 120)
    view = await store.subscription(PRINCIPAL.account, sub.id)
    assert view.github is not None
    assert view.github.access[0].error_since == initial.error_since
    assert view.github.access[0].error_observed_at is not None
    assert initial.error_observed_at is not None
    assert view.github.access[0].error_observed_at >= initial.error_observed_at
    async with store.sessions.begin() as session:
        await session.execute(update(GitHubRepositoryAccess).values(next_attempt=datetime.now(UTC)))
    lease = await state.acquire(key)
    assert lease is not None
    await state.succeed(lease)
    view = await store.subscription(PRINCIPAL.account, sub.id)
    assert view.github is not None
    assert view.github.access[0].currently_valid
    assert view.github.access[0].error is None
    assert view.error is None
    assert not (await store.read(PRINCIPAL.account, sub.inbox_id, 0, 128)).entries


@pytest.mark.parametrize("event", ["installation", "installation_repositories"])
async def test_invalidation_fences_inflight_refresh(
    store: Store, provider: tuple[GitHub, Upstream], event: str
) -> None:
    github, _ = provider
    sub = await store.subscribe(PRINCIPAL, subscription(), (await github.context(SOURCE)).binding)
    state = GitHubState(store.sessions, 600)
    key = AccessKey(42, 11, 100)
    lease = await state.acquire(key)
    assert lease is not None
    await ingest(github, store, {"installation": {"id": 11}, "action": "removed"}, event)
    with pytest.raises(RefreshDeferredError):
        await state.succeed(lease)
    view = await store.subscription(PRINCIPAL.account, sub.id)
    assert view.github is not None
    assert not view.github.access[0].currently_valid
    replacement = await state.acquire(key)
    assert replacement is not None
    await state.succeed(replacement)
    assert await state.acquire(key) is None


@pytest.mark.parametrize("invalidate", [False, True])
async def test_access_fenced_again_at_match_commit(
    store: Store, provider: tuple[GitHub, Upstream], invalidate: bool
) -> None:
    github, _ = provider
    sub = await store.subscribe(PRINCIPAL, subscription(), (await github.context(SOURCE)).binding)
    await ingest(github, store, comment())
    claim = await store.claim()
    assert claim is not None
    row = await store.source(claim)
    assert row is not None
    read = store.github_deliveries

    async def race(source: Subscription, predicate: ColumnElement[bool]) -> list[GitHubDelivery]:
        receipts = await read(source, predicate)
        assert len(receipts) == 1
        if invalidate:
            await ingest(github, store, {"installation": {"id": 11}, "action": "suspend"}, "installation")
        else:
            async with store.sessions.begin() as session:
                await session.execute(update(GitHubRepositoryAccess).values(valid_until=datetime.now(UTC)))
        return receipts

    with patch.object(store, "github_deliveries", side_effect=race):
        await github.reconcile(store, claim, row, SOURCE)
    page = await store.read(PRINCIPAL.account, sub.inbox_id, 0, 128)
    assert not page.entries
    assert page.inbox.last_cursor == 0
    assert page.notice is None
    assert await store.source(claim) is not None


async def test_shared_errors_project_without_copying_into_subscriptions(
    store: Store, provider: tuple[GitHub, Upstream]
) -> None:
    github, upstream = provider
    binding = (await github.context(SOURCE)).binding
    first = await store.subscribe(PRINCIPAL, subscription(), binding)
    second = await store.subscribe(PRINCIPAL, subscription(key="other"), binding)
    upstream.limited = True
    claim = await store.claim()
    assert claim is not None
    for sub in [first, second]:
        async with store.sessions() as session:
            row = await session.get(Subscription, sub.id)
        assert row is not None
        with pytest.raises(RefreshDeferredError) as deferred:
            await github.reconcile(store, claim, row, SOURCE)
        await store.source_deferred(claim, row, deferred.value.until)
    views = await store.subscriptions(PRINCIPAL.account)
    assert len(views) == 2
    assert views[0].github == views[1].github
    assert views[0].github is not None
    assert views[0].github.access[0].error_kind == SourceFailureKind.RATE_LIMITED
    assert all(view.error is None and view.last_success_at is None for view in views)
    async with store.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(GitHubRepositoryAccess)) == 1
        rows = list(await session.scalars(select(Subscription)))
        assert all(row.error is None and row.error_kind is None for row in rows)
    page = await store.read(PRINCIPAL.account, first.inbox_id, 0, 128)
    assert page.inbox.last_cursor == page.inbox.acknowledged == 0
    assert not page.entries
    assert page.notice is None


async def test_webhook_racing_bootstrap_keeps_both_revision_associations(
    store: Store, provider: tuple[GitHub, Upstream]
) -> None:
    github, upstream = provider
    source = SOURCE.model_copy(update={"events": {EventFilter(event=EventName.CHECK_RUN)}})
    sub = await store.subscribe(PRINCIPAL, subscription(source), (await github.context(source)).binding)
    await ingest(github, store, check(NEXT), "check_run")
    await ingest(github, store, check(HEAD), "check_run")
    claim = await store.claim()
    assert claim is not None
    row = await store.source(claim)
    assert row is not None
    request = github.client.request

    async def race(
        method: str,
        path: str,
        headers: dict[str, str],
        *,
        json: dict[str, JsonValue] | None = None,
        allow_missing: bool = False,
    ) -> httpx.Response:
        response = await request(method, path, headers, json=json, allow_missing=allow_missing)
        if path.endswith("/pulls/7"):
            await ingest(github, store, pr(NEXT), "pull_request")
        return response

    with patch.object(github.client, "request", side_effect=race):
        await github.reconcile(store, claim, row, source)
    page = await store.read(PRINCIPAL.account, sub.inbox_id, 0, 128)
    assert [entry.payload for entry in page.entries] == [check(NEXT), check(HEAD)]
    calls = list(upstream.requests)
    await ingest(github, store, pr(HEAD), "pull_request")  # Delayed older observation cannot erase NEXT.
    await ingest(github, store, check(NEXT), "check_run")
    row = await store.source(claim)
    assert row is not None
    await github.reconcile(store, claim, row, source)
    assert upstream.requests == calls
    assert (await store.read(PRINCIPAL.account, sub.inbox_id, 0, 128)).inbox.last_cursor == 3


async def test_access_refresh_is_single_flight_across_workers(
    store: Store, engine: AsyncEngine, provider: tuple[GitHub, Upstream]
) -> None:
    github, upstream = provider
    await store.subscribe(PRINCIPAL, subscription(), (await github.context(SOURCE)).binding)
    entered, resume = asyncio.Event(), asyncio.Event()
    request = github.client.request
    upstream.requests.clear()

    async def paused(
        method: str,
        path: str,
        headers: dict[str, str],
        *,
        json: dict[str, JsonValue] | None = None,
        allow_missing: bool = False,
    ) -> httpx.Response:
        if path == "/repositories/100":
            entered.set()
            await resume.wait()
        return await request(method, path, headers, json=json, allow_missing=allow_missing)

    key = AccessKey(42, 11, 100)
    with patch.object(github.client, "request", side_effect=paused):
        async with asyncio.TaskGroup() as tasks:
            first = tasks.create_task(github.refresh_access(store, key))
            await entered.wait()
            other = GitHub(GitHubClient(github.client.http, github.settings))
            with pytest.raises(RefreshDeferredError):
                await other.refresh_access(Store(engine), key)
            resume.set()
    assert first.result().key == key
    assert upstream.requests.count("/repositories/100") == 1
    assert upstream.requests.count("/repos/owner/repo/installation") == 1


async def test_numeric_repository_identity_survives_rename(store: Store, provider: tuple[GitHub, Upstream]) -> None:
    github, upstream = provider
    sub = await store.subscribe(PRINCIPAL, subscription(), (await github.context(SOURCE)).binding)
    upstream.responses["/repositories/100"] = httpx.Response(200, json={"id": 100, "full_name": "owner/renamed"})
    claim = await store.claim()
    assert claim is not None
    row = await store.source(claim)
    assert row is not None
    await github.reconcile(store, claim, row, SOURCE)
    assert "/repos/owner/renamed/installation" in upstream.requests
    assert "/repos/owner/renamed/pulls/7" in upstream.requests
    async with store.sessions() as session:
        repository = await session.get(GitHubRepository, 100)
        assert repository is not None
        assert repository.full_name == "owner/renamed"
    view = await store.subscription(PRINCIPAL.account, sub.id)
    assert isinstance(view.source, GitHubSource)
    assert view.source.repository == "owner/repo"  # Immutable idempotency input, not current identity.
    assert view.github is not None
    assert view.github.access[0].currently_valid


async def test_access_expiring_during_append_rolls_back_prefix(store: Store, provider: tuple[GitHub, Upstream]) -> None:
    github, _ = provider
    sub = await store.subscribe(PRINCIPAL, subscription(), (await github.context(SOURCE)).binding)
    await ingest(github, store, comment())
    claim = await store.claim()
    assert claim is not None
    row = await store.source(claim)
    assert row is not None
    append = store.append_event

    async def expire(
        session: AsyncSession,
        inbox: Inbox,
        source: Subscription,
        identity: EventIdentity,
        payload: dict[str, JsonValue],
    ) -> None:
        await append(session, inbox, source, identity, payload)
        await session.execute(update(GitHubRepositoryAccess).values(valid_until=datetime.now(UTC)))

    with patch.object(store, "append_event", side_effect=expire), pytest.raises(RefreshDeferredError):
        await github.reconcile(store, claim, row, SOURCE)
    page = await store.read(PRINCIPAL.account, sub.inbox_id, 0, 128)
    assert page.inbox.last_cursor == 0
    assert page.entries == []
    assert page.notice is None


@pytest.mark.parametrize("table", [GitHubRepositoryAccess, GitHubSubject])
async def test_shared_failure_fields_are_constrained(
    store: Store, provider: tuple[GitHub, Upstream], table: type[GitHubRepositoryAccess] | type[GitHubSubject]
) -> None:
    github, _ = provider
    await store.subscribe(PRINCIPAL, subscription(), (await github.context(SOURCE)).binding)
    with pytest.raises(IntegrityError):
        async with store.sessions.begin() as session:
            await session.execute(update(table).values(error="partial observation"))
    with pytest.raises(IntegrityError):
        async with store.sessions.begin() as session:
            await session.execute(
                update(table).values(
                    error="bad kind",
                    error_kind="healthy",
                    error_since=datetime.now(UTC),
                    error_observed_at=datetime.now(UTC),
                )
            )


async def test_normalization_backfills_retained_head_evidence_without_grants(
    store: Store, engine: AsyncEngine, provider: tuple[GitHub, Upstream]
) -> None:
    github, _ = provider
    source = SOURCE.model_copy(update={"events": {EventFilter(event=EventName.CHECK_RUN)}})
    sub = await store.subscribe(PRINCIPAL, subscription(source), (await github.context(source)).binding)
    payload = pr(NEXT)
    payload["pull_request"] = {"number": 7, "head": {"sha": NEXT, "repo": {"id": 200, "full_name": "fork/repo"}}}

    def upgrade_receipt(connection: Connection) -> None:
        config = Config()
        config.set_main_option("script_location", str(RUNNER.migrations_dir))
        config.attributes["connection"] = connection
        command.downgrade(config, "0007_subscription_health")
        connection.execute(
            text("""
            INSERT INTO github_delivery (app_id, delivery_id, installation_id, repository_id, event,
                action, head_sha, subjects, digest, payload, received_at)
            VALUES (42, :delivery, 11, 100, 'pull_request', 'synchronize', :sha,
                ARRAY['pull_request:7'], :digest, CAST(:payload AS jsonb), now())
        """),
            {"delivery": uuid4(), "sha": NEXT, "digest": b"x" * 32, "payload": json.dumps(payload)},
        )
        connection.execute(
            text("""
            INSERT INTO github_delivery (app_id, delivery_id, installation_id, repository_id, event,
                action, head_sha, subjects, digest, payload, received_at)
            VALUES (42, :delivery, 11, 100, 'issue_comment', 'created', NULL,
                ARRAY['pull_request:7', 'pull_request:7'], :digest, CAST(:payload AS jsonb), now())
        """),
            {"delivery": uuid4(), "digest": b"y" * 32, "payload": json.dumps(comment())},
        )
        connection.execute(
            text("""
            INSERT INTO github_delivery (app_id, delivery_id, installation_id, repository_id, event,
                action, head_sha, subjects, digest, payload, received_at)
            VALUES (42, :delivery, 11, 100, 'delete', NULL, NULL,
                ARRAY['branch:release/team.v2'], :digest, CAST(:payload AS jsonb), now())
        """),
            {
                "delivery": uuid4(),
                "digest": b"z" * 32,
                "payload": json.dumps(
                    {
                        "installation": {"id": 11},
                        "repository": {"id": 100, "full_name": "owner/repo"},
                        "ref": "release/team.v2",
                        "ref_type": "branch",
                    }
                ),
            },
        )
        RUNNER.run_for_connection(connection)
        RUNNER.run_for_connection(connection)

    async with engine.begin() as connection:
        await connection.run_sync(upgrade_receipt)
    async with store.sessions() as session:
        revisions = list(await session.scalars(select(GitHubSubjectRevision)))
        assert {(row.head_repository_id, row.sha) for row in revisions} == {(100, NEXT), (200, NEXT)}
        assert await session.scalar(select(func.count()).select_from(GitHubRepositoryAccess)) == 1
        links = list(
            await session.scalars(select(GitHubDeliverySubject).order_by(GitHubDeliverySubject.delivery_position))
        )
        assert [(link.kind, link.subject_key) for link in links] == [
            ("pull_request", "7"),
            ("pull_request", "7"),
            ("branch", "release/team.v2"),
        ]
        assert not await session.scalar(
            text("""
            SELECT EXISTS (SELECT 1 FROM information_schema.columns
                WHERE table_schema = current_schema() AND table_name = 'github_delivery' AND column_name = 'subjects')
        """)
        )
    view = await store.subscription(PRINCIPAL.account, sub.id)
    assert view.github is not None
    assert not view.github.access[0].currently_valid
    assert view.github.access[0].last_success_at is None
    assert view.github.subject.last_success_at is None
    await ingest(github, store, check(NEXT), "check_run")
    claim = await store.claim()
    assert claim is not None
    row = await store.source(claim)
    assert row is not None
    await github.reconcile(store, claim, row, source)
    page = await store.read(PRINCIPAL.account, sub.inbox_id, 0, 128)
    assert [entry.payload for entry in page.entries] == [check(NEXT)]


async def test_delivery_subject_links_preserve_shaless_events_and_reject_cross_repository_links(
    store: Store, provider: tuple[GitHub, Upstream]
) -> None:
    github, _ = provider
    payload = comment()
    raw, headers = signed(payload, "issue_comment")
    delivery_id = UUID(headers["X-GitHub-Delivery"])
    assert await github.ingest(store, "issue_comment", delivery_id, headers["X-Hub-Signature-256"], raw)
    assert not await github.ingest(store, "issue_comment", delivery_id, headers["X-Hub-Signature-256"], raw)
    await ingest(github, store, payload | {"repository": {"id": 200, "full_name": "fork/repo"}}, "issue_comment")
    async with store.sessions() as session:
        receipt = await session.scalar(select(GitHubDelivery).where(GitHubDelivery.delivery_id == delivery_id))
        assert receipt is not None
        assert receipt.head_sha is None
        links = list(
            await session.scalars(
                select(GitHubDeliverySubject).where(GitHubDeliverySubject.delivery_position == receipt.position)
            )
        )
        assert [(link.repository_id, link.kind, link.subject_key) for link in links] == [(100, "pull_request", "7")]
    with pytest.raises(IntegrityError):
        async with store.sessions.begin() as session:
            session.add(
                GitHubDeliverySubject(
                    delivery_position=receipt.position, repository_id=200, kind="pull_request", subject_key="7"
                )
            )


def test_correlation_returns_typed_references_without_sha() -> None:
    assert correlation(IssuePayload.model_validate(comment())) == (
        None,
        [PullRequestSubject(kind="pull_request", number=7)],
    )
    assert correlation(
        RefPayload.model_validate({"installation": {"id": 11}, "ref": "release/team.v2", "ref_type": "branch"})
    ) == (None, [BranchSubject(kind="branch", name="release/team.v2")])


if __name__ == "__main__":
    pytest_bazel.main()
