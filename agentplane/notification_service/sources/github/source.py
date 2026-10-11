"""Signed GitHub ingress and durable notification matching, using the async GitHub client."""

import asyncio
import hashlib
import hmac
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID

import httpx
from pydantic import Field, JsonValue, TypeAdapter
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
from agentplane.notification_service.models import SourceFailureKind
from agentplane.notification_service.sources.github.client import (
    GitHubAccessError,
    GitHubClient,
    GitHubNotInstalledError,
    GitHubRetryError,
    GitHubSourceChangedError,
    GitHubUnavailableError,
    Installation,
    Issue,
    PullRequest,
    Repository,
    Upstream,
)
from agentplane.notification_service.sources.github.models import (
    CI_EVENTS,
    ISSUE_EVENTS,
    PR_EVENTS,
    REF_EVENTS,
    BranchSubject,
    CommitSubject,
    EventName,
    GitHubBinding,
    GitHubEvent,
    GitHubSource,
    IssueSubject,
    PullRequestSubject,
    Subject,
    subject_key,
)
from agentplane.notification_service.sources.github.state import (
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
from agentplane.notification_service.store import Store

logger = logging.getLogger(__name__)

_PAYLOAD = TypeAdapter(dict[str, JsonValue])
SUPPORTED_EVENTS = CI_EVENTS | PR_EVENTS | REF_EVENTS | ISSUE_EVENTS


class InvalidSignatureError(Exception):
    pass


class UnsupportedWebhookEventError(ValueError):
    pass


class MissingWebhookRepositoryError(ValueError):
    pass


class Envelope(Upstream):
    installation: Installation
    repository: Repository | None = None
    action: str | None = Field(default=None, max_length=64)


class PullRequestPayload(Envelope):
    pull_request: PullRequest


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


class WorkflowJob(Upstream):
    head_sha: str
    head_branch: str | None = None


class WorkflowJobPayload(Envelope):
    workflow_job: WorkflowJob


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
    EventName.ISSUES: IssuePayload,
    EventName.CHECK_RUN: CheckRunPayload,
    EventName.CHECK_SUITE: CheckSuitePayload,
    EventName.WORKFLOW_RUN: WorkflowPayload,
    EventName.WORKFLOW_JOB: WorkflowJobPayload,
    EventName.STATUS: StatusPayload,
    EventName.PUSH: PushPayload,
    EventName.CREATE: RefPayload,
    EventName.DELETE: RefPayload,
    "installation": Envelope,
    "installation_repositories": Envelope,
}


def correlation(payload: Envelope) -> tuple[str | None, list[Subject]]:
    """Index upstream subject references without imposing a universal filter vocabulary."""
    match payload:
        case PullRequestPayload(pull_request=pr):
            return pr.head.sha, [PullRequestSubject(kind="pull_request", number=pr.number)]
        case IssuePayload(issue=issue) if issue.pull_request is not None:
            return None, [PullRequestSubject(kind="pull_request", number=issue.number)]
        case IssuePayload(issue=issue):
            return None, [IssueSubject(kind="issue", number=issue.number)]
        case WorkflowJobPayload(workflow_job=job):
            job_subjects: list[Subject] = []
            if job.head_branch is not None:
                job_subjects.append(BranchSubject(kind="branch", name=job.head_branch))
            return job.head_sha, job_subjects
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
    def __init__(self, client: GitHubClient) -> None:
        self.client, self.settings = client, client.settings
        self.ingress_slots = asyncio.Semaphore(self.settings.webhook_concurrency)

    async def repository(self, name: str) -> GitHubBinding:
        installation = await self.client.installation(name)
        repository = await self.client.repository(name, installation.id)
        return GitHubBinding(app_id=self.settings.app_id, installation_id=installation.id, repository_id=repository.id)

    async def context(self, source: GitHubSource) -> Context:
        binding = await self.repository(source.repository)
        context = Context(binding, {binding.repository_id: binding.installation_id}, set())
        match source.subject:
            case PullRequestSubject(number=number):
                pr = await self.client.pull_request(source.repository, number, binding.installation_id)
                context.heads.add(pr.head.sha)
                if pr.head.repo is not None and pr.head.repo.id != binding.repository_id:
                    # A base installation does not grant authority over an uninstalled fork.
                    try:
                        fork = await self.repository(pr.head.repo.full_name)
                    except GitHubNotInstalledError as error:
                        logger.warning("PR fork %s is not covered: %s", pr.head.repo.full_name, error)
                    else:
                        if fork.repository_id != pr.head.repo.id:
                            raise GitHubSourceChangedError("GitHub fork repository identity changed")
                        context.installations[fork.repository_id] = fork.installation_id
            case IssueSubject(number=number):
                await self.client.issue(source.repository, number, binding.installation_id)
            case BranchSubject(name=name):
                branch = await self.client.branch(source.repository, name, binding.installation_id)
                if branch is not None:
                    context.heads.add(branch.object.sha)
            case CommitSubject(sha=sha):
                await self.client.commit(source.repository, sha, binding.installation_id)
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
            raise UnsupportedWebhookEventError("unsupported GitHub webhook event")
        payload = _PAYLOAD.validate_json(raw)
        envelope = PAYLOAD_MODELS[event].model_validate(payload)
        if event in SUPPORTED_EVENTS and envelope.repository is None:
            raise MissingWebhookRepositoryError("repository event requires a repository")
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
                repository = await self.client.repository_by_id(key.repository_id, key.installation_id)
                installation = await self.client.installation(repository.full_name)
                if repository.id != key.repository_id or installation.id != key.installation_id:
                    raise GitHubSourceChangedError("GitHub repository/installation identity changed")
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
            async with store.sessions() as session:
                repository = await session.get(GitHubRepository, key.repository_id)
                assert repository is not None
                name = repository.full_name
            match source.subject:
                case PullRequestSubject(number=number):
                    pr = await self.client.pull_request(name, number, base.key.installation_id)
                    # Base-repository CI may build the fork head. The fork association requires
                    # an explicit upstream repository identity; SHA equality does not grant access.
                    revisions.append(HeadRevision(key.repository_id, name, pr.head.sha))
                    if pr.head.repo is not None and pr.head.repo.id != key.repository_id:
                        revisions.append(HeadRevision(pr.head.repo.id, pr.head.repo.full_name, pr.head.sha))
                        try:
                            fork = await self.repository(pr.head.repo.full_name)
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
                case IssueSubject(number=number):
                    await self.client.issue(name, number, base.key.installation_id)
                case BranchSubject(name=branch):
                    ref = await self.client.branch(name, branch, base.key.installation_id)
                    if ref is not None:
                        revisions.append(HeadRevision(key.repository_id, name, ref.object.sha))
                case CommitSubject(sha=sha):
                    await self.client.commit(name, sha, base.key.installation_id)
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
