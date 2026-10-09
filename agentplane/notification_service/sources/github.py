"""GitHub App authentication, signed ingress, and provider-owned subject matching."""

import asyncio
import hashlib
import hmac
import logging
import math
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Literal
from urllib.parse import quote
from uuid import UUID

import httpx
import jwt
from pydantic import BaseModel, ConfigDict, Field, JsonValue, SecretStr, TypeAdapter
from sqlalchemy import ColumnElement, false, or_, select, true
from sqlalchemy.dialects.postgresql import insert

from agentplane.notification_service.db import (
    GitHubDelivery,
    GitHubDeliverySubject,
    GitHubInstallation,
    GitHubRepository,
    GitHubRepositoryAccess,
    GitHubSubjectRevision,
    Inbox,
    Subscription,
)
from agentplane.notification_service.github_state import (
    AccessFence,
    AccessKey,
    GitHubState,
    HeadRevision,
    RefreshDeferredError,
    RefreshLease,
    SubjectKey,
    access_valid,
    github_lock,
)
from agentplane.notification_service.models import SourceFailureKind
from agentplane.notification_service.settings import GitHubSettings
from agentplane.notification_service.sources.github_models import (
    CI_EVENTS,
    PR_EVENTS,
    REF_EVENTS,
    BranchSubject,
    CommitSubject,
    EventName,
    GitHubBinding,
    GitHubEvent,
    GitHubSource,
    PullRequestSubject,
    RepositoryName,
    Subject,
    subject_key,
)
from agentplane.notification_service.store import Store

# RS256 is loaded dynamically by PyJWT.
# gazelle:include_dep @pypi//cryptography

logger = logging.getLogger(__name__)

_PAYLOAD = TypeAdapter(dict[str, JsonValue])
SUPPORTED_EVENTS = CI_EVENTS | PR_EVENTS | REF_EVENTS


class GitHubUnavailableError(Exception):
    """Disabled provider or confirmed loss of repository/installation access."""


class GitHubAccessError(GitHubUnavailableError):
    """Access is denied or the installation is suspended."""


class GitHubSourceChangedError(GitHubUnavailableError):
    """The authorized repository, installation or commit identity changed."""


class GitHubNotInstalledError(GitHubAccessError):
    """The App's installation lookup returned 404 for this repository."""


class GitHubRetryError(Exception):
    def __init__(self, status_code: int, retry_seconds: int) -> None:
        self.retry_seconds = retry_seconds
        super().__init__(f"GitHub rate limited (HTTP {status_code}); retry in {retry_seconds}s")


def rate_limit_delay(headers: httpx.Headers, now: float) -> int:
    # GitHub primary limits use an epoch reset; secondary limits may supply Retry-After.
    # Neither deadline may be shortened by our fallback or a maximum-delay clamp.
    delay = 60
    retry_after = headers.get("retry-after", "")
    if retry_after.isascii() and retry_after.isdecimal():
        delay = max(delay, int(retry_after))
    elif retry_after:
        try:
            deadline = parsedate_to_datetime(retry_after)
        except ValueError, OverflowError:
            pass
        else:
            if deadline.tzinfo is not None:
                delay = max(delay, math.ceil(deadline.timestamp() - now))
    reset = headers.get("x-ratelimit-reset", "")
    if headers.get("x-ratelimit-remaining") == "0" and reset.isascii() and reset.isdecimal():
        delay = max(delay, math.ceil(int(reset) - now))
    return delay


class InvalidSignatureError(Exception):
    pass


class Upstream(BaseModel):
    model_config = ConfigDict(extra="ignore", hide_input_in_errors=True)


class Installation(Upstream):
    id: int = Field(gt=0)
    suspended_at: datetime | None = None


class Repository(Upstream):
    id: int = Field(gt=0)
    full_name: RepositoryName


class Envelope(Upstream):
    installation: Installation
    repository: Repository | None = None
    action: str | None = Field(default=None, max_length=64)


class Revision(Upstream):
    sha: str
    repo: Repository | None = None


class PullRequest(Upstream):
    number: int
    head: Revision


class PullRequestPayload(Envelope):
    pull_request: PullRequest


class Issue(Upstream):
    number: int
    pull_request: dict[str, JsonValue] | None = None


class IssuePayload(Envelope):
    issue: Issue


class PullReference(Upstream):
    number: int


class Check(Upstream):
    head_sha: str
    pull_requests: list[PullReference] = Field(default_factory=list)


class CheckRunPayload(Envelope):
    check_run: Check


class CheckSuitePayload(Envelope):
    check_suite: Check


class Workflow(Check):
    head_branch: str | None = None


class WorkflowPayload(Envelope):
    workflow_run: Workflow


class Branch(Upstream):
    name: str


class StatusPayload(Envelope):
    sha: str
    branches: list[Branch] = Field(default_factory=list)


class PushPayload(Envelope):
    ref: str
    after: str


class RefPayload(Envelope):
    ref: str
    ref_type: Literal["branch", "tag"]


class GitRef(Upstream):
    object: Revision


class Commit(Upstream):
    sha: str


class Token(Upstream):
    token: SecretStr
    expires_at: datetime


@dataclass
class Context:
    binding: GitHubBinding
    installations: dict[int, int]
    heads: set[str]


# GitHub supplies the discriminator in a header, not in the JSON body.
PAYLOAD_MODELS: dict[str, type[Envelope]] = {
    EventName.PULL_REQUEST: PullRequestPayload,
    EventName.PULL_REQUEST_REVIEW: PullRequestPayload,
    EventName.PULL_REQUEST_REVIEW_COMMENT: PullRequestPayload,
    EventName.ISSUE_COMMENT: IssuePayload,
    EventName.CHECK_RUN: CheckRunPayload,
    EventName.CHECK_SUITE: CheckSuitePayload,
    EventName.WORKFLOW_RUN: WorkflowPayload,
    EventName.STATUS: StatusPayload,
    EventName.PUSH: PushPayload,
    EventName.CREATE: RefPayload,
    EventName.DELETE: RefPayload,
    "installation": Envelope,
    "installation_repositories": Envelope,
}


def api_headers(bearer: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {bearer}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def correlation(payload: Envelope) -> tuple[str | None, list[Subject]]:
    """Index upstream subject references without imposing a universal filter vocabulary."""
    match payload:
        case PullRequestPayload(pull_request=pr):
            return pr.head.sha, [PullRequestSubject(kind="pull_request", number=pr.number)]
        case IssuePayload(issue=issue) if issue.pull_request is not None:
            return None, [PullRequestSubject(kind="pull_request", number=issue.number)]
        case PushPayload(ref=ref, after=sha) if ref.startswith("refs/heads/"):
            return (None if sha == "0" * 40 else sha), [
                BranchSubject(kind="branch", name=ref.removeprefix("refs/heads/"))
            ]
        case RefPayload(ref=ref, ref_type="branch"):
            return None, [BranchSubject(kind="branch", name=ref)]
        case CheckRunPayload(check_run=check) | CheckSuitePayload(check_suite=check):
            return check.head_sha, [
                PullRequestSubject(kind="pull_request", number=pr.number) for pr in check.pull_requests
            ]
        case WorkflowPayload(workflow_run=workflow):
            subjects: list[Subject] = [
                PullRequestSubject(kind="pull_request", number=pr.number) for pr in workflow.pull_requests
            ]
            if workflow.head_branch is not None:
                subjects.append(BranchSubject(kind="branch", name=workflow.head_branch))
            return workflow.head_sha, subjects
        case StatusPayload(sha=sha, branches=branches):
            return sha, [BranchSubject(kind="branch", name=branch.name) for branch in branches]
        case _:
            return None, []


class GitHub:
    def __init__(self, http: httpx.AsyncClient, settings: GitHubSettings) -> None:
        self.http, self.settings = http, settings
        self.tokens: dict[int, Token] = {}
        self.token_lock = asyncio.Lock()
        self.ingress_slots = asyncio.Semaphore(settings.webhook_concurrency)

    def app_headers(self) -> dict[str, str]:
        private_key = self.settings.private_key.get_secret_value()
        now = int(time.time())
        bearer = jwt.encode(
            {"iat": now - 30, "exp": now + 540, "iss": str(self.settings.app_id)}, private_key, algorithm="RS256"
        )
        return api_headers(bearer)

    def start(self) -> None:
        # Validate the App private key before HTTP readiness; Settings validates the signing secret.
        self.app_headers()

    async def request(
        self,
        method: str,
        path: str,
        headers: dict[str, str],
        *,
        json: dict[str, JsonValue] | None = None,
        allow_missing: bool = False,
    ) -> httpx.Response:
        # No redirects: credentials must never follow repository redirects to another origin.
        response = await self.http.request(method, path, headers=headers, json=json)
        if response.status_code == 429 or (
            response.status_code == 403
            and (response.headers.get("x-ratelimit-remaining") == "0" or "retry-after" in response.headers)
        ):
            raise GitHubRetryError(response.status_code, rate_limit_delay(response.headers, time.time()))
        if allow_missing and response.status_code == 404:
            return response
        if response.status_code == 401:
            self.tokens.clear()
        if response.status_code in (401, 403, 404):
            raise GitHubAccessError(
                f"GitHub App access unavailable (HTTP {response.status_code}); check credentials and permissions"
            )
        response.raise_for_status()
        return response

    async def installation_headers(self, installation_id: int) -> dict[str, str]:
        async with self.token_lock:
            token = self.tokens.get(installation_id)
            if token is None or token.expires_at <= datetime.now(UTC) + timedelta(minutes=5):
                response = await self.request(
                    "POST",
                    f"/app/installations/{installation_id}/access_tokens",
                    self.app_headers(),
                    json={"permissions": {"metadata": "read", "contents": "read", "pull_requests": "read"}},
                )
                token = Token.model_validate_json(response.content)
                self.tokens[installation_id] = token
        return api_headers(token.token.get_secret_value())

    async def repository(self, name: str) -> tuple[GitHubBinding, dict[str, str]]:
        response = await self.request("GET", f"/repos/{name}/installation", self.app_headers(), allow_missing=True)
        if response.status_code == 404:
            raise GitHubNotInstalledError("GitHub App has no accessible installation for this repository")
        installation = Installation.model_validate_json(response.content)
        if installation.suspended_at is not None:
            raise GitHubAccessError("GitHub App installation is suspended")
        headers = await self.installation_headers(installation.id)
        repository = Repository.model_validate_json((await self.request("GET", f"/repos/{name}", headers)).content)
        return GitHubBinding(
            app_id=self.settings.app_id, installation_id=installation.id, repository_id=repository.id
        ), headers

    async def context(self, source: GitHubSource) -> Context:
        binding, headers = await self.repository(source.repository)
        context = Context(binding, {binding.repository_id: binding.installation_id}, set())
        match source.subject:
            case PullRequestSubject(number=number):
                pr = PullRequest.model_validate_json(
                    (await self.request("GET", f"/repos/{source.repository}/pulls/{number}", headers)).content
                )
                context.heads.add(pr.head.sha)
                if pr.head.repo is not None and pr.head.repo.id != binding.repository_id:
                    # A base installation does not grant authority over an uninstalled fork.
                    try:
                        fork, _ = await self.repository(pr.head.repo.full_name)
                    except GitHubNotInstalledError as error:
                        logger.warning("PR fork %s is not covered: %s", pr.head.repo.full_name, error)
                    else:
                        if fork.repository_id != pr.head.repo.id:
                            raise GitHubSourceChangedError("GitHub fork repository identity changed")
                        context.installations[fork.repository_id] = fork.installation_id
            case BranchSubject(name=name):
                response = await self.request(
                    "GET",
                    f"/repos/{source.repository}/git/ref/heads/{quote(name, safe='')}",
                    headers,
                    allow_missing=True,
                )
                if response.status_code != 404:
                    response.raise_for_status()
                    context.heads.add(GitRef.model_validate_json(response.content).object.sha)
            case CommitSubject(sha=sha):
                commit = Commit.model_validate_json(
                    (await self.request("GET", f"/repos/{source.repository}/commits/{sha}", headers)).content
                )
                if commit.sha != sha:
                    raise GitHubSourceChangedError("GitHub commit identity changed")
                context.heads.add(sha)
        return context

    async def ingest(self, store: Store, event: str, delivery_id: UUID, signature: str, raw: bytes) -> bool:
        expected = (
            "sha256="
            + hmac.new(self.settings.webhook_secret.get_secret_value().encode(), raw, hashlib.sha256).hexdigest()
        )
        if not signature.isascii() or not hmac.compare_digest(expected, signature):
            raise InvalidSignatureError
        if event == "ping":
            return True
        if event not in PAYLOAD_MODELS:
            raise ValueError("unsupported GitHub webhook event")
        payload = _PAYLOAD.validate_json(raw)
        envelope = PAYLOAD_MODELS[event].model_validate(payload)
        if event in SUPPORTED_EVENTS and envelope.repository is None:
            raise ValueError("repository event requires a repository")
        sha, subjects = correlation(envelope)
        keys = []
        revisions = []
        if envelope.repository is not None:
            repository = envelope.repository
            keys = [SubjectKey(repository.id, subject.kind, subject_key(subject)) for subject in subjects]
            if sha is not None:
                revisions.append(HeadRevision(repository.id, repository.full_name, sha))
                if isinstance(envelope, PullRequestPayload) and envelope.pull_request.head.repo is not None:
                    head = envelope.pull_request.head.repo
                    revisions.append(HeadRevision(head.id, head.full_name, sha))
        return await store.ingest_github(
            self.settings.app_id,
            delivery_id,
            envelope.installation.id,
            envelope.repository.id if envelope.repository else None,
            event,
            hashlib.sha256(event.encode() + b"\0" + raw).digest(),
            payload,
            action=envelope.action,
            head_sha=sha,
            subjects=keys,
            repository_name=envelope.repository.full_name if envelope.repository else None,
            revisions=revisions,
        )

    async def refresh_failed(self, state: GitHubState, lease: RefreshLease, error: Exception) -> None:
        kind = SourceFailureKind.PROCESSING_ERROR
        if isinstance(error, GitHubRetryError):
            kind = SourceFailureKind.RATE_LIMITED
        elif isinstance(error, GitHubAccessError):
            kind = SourceFailureKind.ACCESS_DENIED
        elif isinstance(error, GitHubSourceChangedError):
            kind = SourceFailureKind.SOURCE_CHANGED
        elif isinstance(error, httpx.TransportError) or (
            isinstance(error, httpx.HTTPStatusError) and error.response.status_code >= 500
        ):
            kind = SourceFailureKind.UNAVAILABLE
        message = str(error) if isinstance(error, (GitHubUnavailableError, GitHubRetryError)) else type(error).__name__
        delay = error.retry_seconds if isinstance(error, GitHubRetryError) else 60
        until = await state.fail(lease, kind, message, delay)
        logger.warning("GitHub shared refresh failed: key=%s cause=%s retry_seconds=%s", lease.key, message, delay)
        raise RefreshDeferredError(until)

    async def refresh_access(self, store: Store, key: AccessKey) -> AccessFence:
        state = GitHubState(store.sessions, self.settings.freshness_seconds)
        lease = await state.acquire(key)
        if lease is not None:
            try:
                headers = await self.installation_headers(key.installation_id)
                repository = Repository.model_validate_json(
                    (await self.request("GET", f"/repositories/{key.repository_id}", headers)).content
                )
                installation = Installation.model_validate_json(
                    (
                        await self.request("GET", f"/repos/{repository.full_name}/installation", self.app_headers())
                    ).content
                )
                if repository.id != key.repository_id or installation.id != key.installation_id:
                    raise GitHubSourceChangedError("GitHub repository/installation identity changed")
                if installation.suspended_at is not None:
                    raise GitHubAccessError("GitHub App installation is suspended")
            except (httpx.HTTPError, ValueError, GitHubUnavailableError, GitHubRetryError) as error:
                await self.refresh_failed(state, lease, error)
            await state.succeed(lease, repository_name=repository.full_name)
        async with store.sessions() as session:
            row = await key.load(session)
            if not await access_valid(session, row, datetime.now(UTC)):
                raise RefreshDeferredError(datetime.now(UTC))
            assert row.validated_installation_generation is not None
            return AccessFence(key, row.generation, row.validated_installation_generation)

    async def refresh_subject(self, store: Store, source: GitHubSource, base: AccessFence) -> SubjectKey:
        key = SubjectKey(base.key.repository_id, source.subject.kind, subject_key(source.subject))
        state = GitHubState(store.sessions, self.settings.freshness_seconds)
        lease = await state.acquire(key)
        if lease is None:
            return key
        revisions = []
        try:
            headers = await self.installation_headers(base.key.installation_id)
            async with store.sessions() as session:
                repository = await session.get(GitHubRepository, key.repository_id)
                assert repository is not None
                name = repository.full_name
            match source.subject:
                case PullRequestSubject(number=number):
                    pr = PullRequest.model_validate_json(
                        (await self.request("GET", f"/repos/{name}/pulls/{number}", headers)).content
                    )
                    # Base-repository CI may build the fork head. The fork association requires
                    # an explicit upstream repository identity; SHA equality does not grant access.
                    revisions.append(HeadRevision(key.repository_id, name, pr.head.sha))
                    if pr.head.repo is not None and pr.head.repo.id != key.repository_id:
                        revisions.append(HeadRevision(pr.head.repo.id, pr.head.repo.full_name, pr.head.sha))
                        try:
                            fork, _ = await self.repository(pr.head.repo.full_name)
                        except GitHubNotInstalledError:
                            pass
                        else:
                            if fork.repository_id != pr.head.repo.id:
                                raise GitHubSourceChangedError("GitHub fork repository identity changed")
                            async with store.sessions.begin() as session:
                                await github_lock(session)
                                await session.execute(
                                    insert(GitHubInstallation)
                                    .values(app_id=fork.app_id, installation_id=fork.installation_id)
                                    .on_conflict_do_nothing()
                                )
                                await session.execute(
                                    insert(GitHubRepository)
                                    .values(repository_id=fork.repository_id, full_name=pr.head.repo.full_name)
                                    .on_conflict_do_nothing()
                                )
                                await session.execute(
                                    insert(GitHubRepositoryAccess)
                                    .values(
                                        app_id=fork.app_id,
                                        installation_id=fork.installation_id,
                                        repository_id=fork.repository_id,
                                    )
                                    .on_conflict_do_nothing()
                                )
                case BranchSubject(name=branch):
                    response = await self.request(
                        "GET", f"/repos/{name}/git/ref/heads/{quote(branch, safe='')}", headers, allow_missing=True
                    )
                    if response.status_code != 404:
                        revisions.append(
                            HeadRevision(
                                key.repository_id, name, GitRef.model_validate_json(response.content).object.sha
                            )
                        )
                case CommitSubject(sha=sha):
                    commit = Commit.model_validate_json(
                        (await self.request("GET", f"/repos/{name}/commits/{sha}", headers)).content
                    )
                    if commit.sha != sha:
                        raise GitHubSourceChangedError("GitHub commit identity changed")
                    revisions.append(HeadRevision(key.repository_id, name, sha))
        except (httpx.HTTPError, ValueError, GitHubUnavailableError, GitHubRetryError) as error:
            await self.refresh_failed(state, lease, error)
        await state.succeed(lease, fences=[base], revisions=revisions)
        return key

    async def reconcile(self, store: Store, claim: Inbox, subscription: Subscription, source: GitHubSource) -> None:
        binding = GitHubBinding.model_validate(subscription.github_binding)
        if binding.app_id != self.settings.app_id:
            raise GitHubSourceChangedError("GitHub App changed; recreate the subscription")
        base = await self.refresh_access(
            store, AccessKey(binding.app_id, binding.installation_id, binding.repository_id)
        )
        key = await self.refresh_subject(store, source, base)
        fences = [base]
        async with store.sessions() as session:
            subject = await key.load(session)
            if subject.last_success_at is None:
                raise RefreshDeferredError(datetime.now(UTC))
            repair_at = subject.last_success_at + timedelta(seconds=self.settings.freshness_seconds)
            subject_generation = subject.generation
            revisions = select(GitHubSubjectRevision.head_repository_id).where(
                GitHubSubjectRevision.repository_id == key.repository_id,
                GitHubSubjectRevision.kind == key.kind,
                GitHubSubjectRevision.subject_key == key.subject_key,
            )
            grants = list(
                await session.scalars(
                    select(GitHubRepositoryAccess).where(
                        GitHubRepositoryAccess.app_id == binding.app_id,
                        GitHubRepositoryAccess.repository_id.in_(revisions),
                        GitHubRepositoryAccess.repository_id != binding.repository_id,
                    )
                )
            )
        for grant in grants:
            try:
                fences.append(
                    await self.refresh_access(
                        store, AccessKey(grant.app_id, grant.installation_id, grant.repository_id)
                    )
                )
            except RefreshDeferredError as deferred:
                repair_at = min(repair_at, deferred.until)
        async with store.sessions() as session:
            for fence in fences:
                access = await fence.key.load(session)
                if access.valid_until is None:
                    raise RefreshDeferredError(datetime.now(UTC))
                repair_at = min(repair_at, access.valid_until)
        delivery = GitHubDelivery
        direct: ColumnElement[bool] = false()
        if not isinstance(source.subject, CommitSubject):
            direct = (
                select(GitHubDeliverySubject.delivery_position)
                .where(
                    GitHubDeliverySubject.delivery_position == delivery.position,
                    GitHubDeliverySubject.repository_id == key.repository_id,
                    GitHubDeliverySubject.kind == key.kind,
                    GitHubDeliverySubject.subject_key == key.subject_key,
                )
                .exists()
            )
        revision = GitHubSubjectRevision
        heads = (
            select(revision.sha)
            .where(
                revision.repository_id == key.repository_id,
                revision.kind == key.kind,
                revision.subject_key == key.subject_key,
                revision.head_repository_id == delivery.repository_id,
                revision.sha == delivery.head_sha,
            )
            .exists()
        )
        selected = or_(
            *(
                (delivery.event == selector.event)
                & (delivery.action.in_(selector.actions) if selector.actions is not None else true())
                for selector in source.filters
            )
        )
        accessible = or_(
            *(
                (delivery.repository_id == fence.key.repository_id)
                & (delivery.installation_id == fence.key.installation_id)
                for fence in fences
            )
        )
        deliveries = await store.github_deliveries(
            subscription,
            (delivery.app_id == binding.app_id)
            & accessible
            & selected
            & (direct | (delivery.event.in_(CI_EVENTS) & heads)),
        )
        matched = []
        for receipt in deliveries:
            assert receipt.repository_id is not None
            matched.append(
                (
                    receipt,
                    GitHubEvent(
                        provider="github",
                        app_id=receipt.app_id,
                        delivery_id=receipt.delivery_id,
                        repository_id=receipt.repository_id,
                        event=EventName(receipt.event),
                        action=receipt.action,
                    ),
                )
            )
        await store.record_github(
            claim,
            subscription,
            matched,
            more=len(deliveries) == 128,
            fences=fences,
            subject_key=key,
            subject_generation=subject_generation,
            repair_at=repair_at,
        )
