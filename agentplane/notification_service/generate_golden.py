"""Golden vectors that pin the Go store (`agentplane/notification_service/store`) to the stored forms of this one.

Stored rows hold values this service computed or serialized: the inbox's `destination_key`, a subscription's
`creation` JSON, an entry's `event` JSON, a notice's `text`, the `EventEntry` bytes of `notice.runner_entry`, and
what a GitHub webhook delivery is recorded as (its digest, action, head SHA, subject keys and head revisions).
Each vector is produced by the Python code that writes the column, and the Go store must reproduce it and read
it back. A `creation` the Python model rejects is a vector too, as is whether two `creation` values name the
same subscription, and a delivery the Python ingest refuses.

Regenerate with `bb run //agentplane/notification_service:generate_golden_bin -- <path to golden.json>`.
"""

import asyncio
import dataclasses
import hashlib
import hmac
import json
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID, uuid5

import httpx
from google.protobuf import json_format
from google.protobuf.timestamp_pb2 import Timestamp
from pydantic import JsonValue, SecretStr, ValidationError

from agentplane.notification_service.db import Subscription
from agentplane.notification_service.models import ActionsEvent, DestinationRef, Subscribe
from agentplane.notification_service.settings import GitHubSettings
from agentplane.notification_service.sources.github.client import GitHubClient
from agentplane.notification_service.sources.github.models import (
    BranchSubject,
    CommitSubject,
    EventName,
    GitHubEvent,
    IssueSubject,
    PullRequestSubject,
    Subject,
    subject_key,
)
from agentplane.notification_service.sources.github.source import (
    GitHub,
    MissingWebhookRepositoryError,
    UnsupportedWebhookEventError,
)
from agentplane.notification_service.sources.github.state import HeadRevision, SubjectKey
from agentplane.notification_service.store import Store, destination_key, notice_text
from agentplane.proto import command_pb2, event_log_pb2, event_pb2
from util.sqlalchemy_types import PydanticColumn

Json = dict[str, object]

REQUEST = "5b9f1a2e-3c4d-4e5f-8a6b-7c8d9e0f1a2b"
OTHER_REQUEST = "6c0a2b3f-4d5e-4f60-9b7c-8d9e0f1a2b3c"
DELIVERY = "8d3c1f2a-5b6e-4c7d-9e0f-1a2b3c4d5e6f"
INBOX = "9e2f3a4b-5c6d-4e7f-80a1-b2c3d4e5f607"
# In capitals, which `str(UUID)` writes back in lowercase: the form History echoes in a receipt.
COMMAND = "7D1E2F3A-4B5C-4D6E-8F70-91A2B3C4D5E6"


def creation_column() -> PydanticColumn[Subscribe]:
    column_type = Subscription.__table__.c.creation.type
    assert isinstance(column_type, PydanticColumn)
    return column_type


CREATION = creation_column()


def destination(namespace: str = "test-namespace", name: str = "test-sandbox", uid: str = "test-uid") -> Json:
    return {"namespace": namespace, "name": name, "uid": uid}


def body(source: Json, **fields: object) -> Json:
    return {
        "destination_ref": destination(),
        "session_id": "test-session",
        "idempotency_key": "test-key",
        "source": source,
    } | fields


def actions(**fields: object) -> Json:
    return {"provider": "actions", "request_id": REQUEST} | fields


def github(subject: Json, **fields: object) -> Json:
    return {"provider": "github", "repository": "test-owner/test-repo", "subject": subject} | fields


PULL_REQUEST: Json = {"kind": "pull_request", "number": 123}
ISSUE: Json = {"kind": "issue", "number": 7}
BRANCH: Json = {"kind": "branch", "name": "feature/test-branch"}
COMMIT: Json = {"kind": "commit", "sha": "0123456789abcdef0123456789abcdef01234567"}


def filter_(event: str, *event_actions: str) -> Json:
    return {"event": event} | ({"actions": list(event_actions)} if event_actions else {})


def stored(subscribe: Subscribe) -> Json:
    result = CREATION.process_bind_param(subscribe, None)
    assert isinstance(result, dict)
    return result


def round_trip(subscribe: Subscribe) -> Subscribe:
    result = CREATION.process_result_value(stored(subscribe), None)
    assert isinstance(result, Subscribe)
    return result


def destination_keys() -> list[Json]:
    cases = {
        "plain ASCII": (destination(), "test-session"),
        "accented letters": (destination(name="sandbøx-é"), "séssion"),
        "CJK": (destination(namespace="名前空間", name="サンドボックス"), "セッション"),
        "astral characters become surrogate pairs": (destination(), "session-🚀-𝄞"),
        "quote and backslash": (destination(), 'session "quoted" \\ back'),
        "short escapes": (destination(), "line\nreturn\rtab\tback\bform\f"),
        "other control characters": (destination(), "nul\x00 soh\x01 unit-separator\x1f"),
        "DEL is escaped": (destination(), "del\x7f"),
        "line and paragraph separators": (destination(), "ls\u2028ps\u2029"),
        "characters JSON libraries escape": (destination(), "a/b <tag> & 'q'"),
        "longest session id": (destination(), "s" * 200),
        "longest destination": (destination(namespace="n" * 63, name="m" * 63, uid="u" * 128), "test-session"),
        "multibyte at the length limit": (destination(namespace="é" * 63, name="名" * 63, uid="🚀" * 128), "x"),
    }
    return [
        {
            "name": name,
            "destination_ref": ref,
            "session_id": session_id,
            "key": destination_key(DestinationRef.model_validate(ref), session_id),
        }
        for name, (ref, session_id) in cases.items()
    ]


def valid_subscriptions() -> dict[str, Json]:
    return {
        "Action request with every default": body(actions()),
        "Action request, explicit values": body(actions(after_sequence=5), lifetime_days=30),
        "Action request, highest after_sequence": body(actions(after_sequence=2**31 - 1), lifetime_days=1),
        "unicode destination": body(actions())
        | {"destination_ref": destination(name="サンドボックス-é"), "session_id": "🚀"},
        "pull request with default events": body(github(PULL_REQUEST)),
        "pull request with explicit null events": body(github(PULL_REQUEST, events=None)),
        "issue with default events": body(github(ISSUE)),
        "branch with default events": body(github(BRANCH)),
        "commit with default events": body(github(COMMIT)),
        "events out of order, repeated and with unordered actions": body(
            github(
                PULL_REQUEST,
                events=[
                    filter_("pull_request_review", "submitted", "edited", "dismissed"),
                    filter_("check_run", "completed"),
                    filter_("issue_comment"),
                    filter_("check_run", "completed"),
                    filter_("pull_request_review", "dismissed", "edited", "submitted", "edited"),
                    filter_("check_run"),
                    filter_("status"),
                ],
            ),
            lifetime_days=14,
        ),
        "issue events": body(github(ISSUE, events=[filter_("issues", "opened", "closed"), filter_("issue_comment")])),
        "branch ref events": body(github(BRANCH, events=[filter_("push"), filter_("delete"), filter_("create")])),
        "commit CI events": body(
            github(
                COMMIT, events=[filter_("workflow_run", "completed"), filter_("workflow_job"), filter_("check_suite")]
            )
        ),
        "unicode branch": body(github({"kind": "branch", "name": "feature/ü-🚀"})),
        "longest repository": body(github(PULL_REQUEST, repository="o" * 100 + "/" + "r" * 99)),
        "32 filters": body(github(COMMIT, events=[filter_("check_run", "a" * i) for i in range(1, 33)])),
    }


def invalid_subscriptions() -> dict[str, Json]:
    valid_pr = github(PULL_REQUEST)
    return {
        "lifetime below 1": body(actions(), lifetime_days=0),
        "lifetime above 30": body(actions(), lifetime_days=31),
        "empty session id": body(actions()) | {"session_id": ""},
        "session id above 200 characters": body(actions()) | {"session_id": "s" * 201},
        "empty idempotency key": body(actions()) | {"idempotency_key": ""},
        "idempotency key above 200 characters": body(actions()) | {"idempotency_key": "k" * 201},
        "empty namespace": body(actions()) | {"destination_ref": destination(namespace="")},
        "namespace above 63 characters": body(actions()) | {"destination_ref": destination(namespace="n" * 64)},
        "multibyte name above 63 characters": body(actions()) | {"destination_ref": destination(name="名" * 64)},
        "uid above 128 characters": body(actions()) | {"destination_ref": destination(uid="u" * 129)},
        "extra top-level field": body(actions()) | {"extra": 1},
        "extra source field": body(actions(extra=1)),
        "missing source": {k: v for k, v in body(actions()).items() if k != "source"},
        "unknown provider": body({"provider": "unknown"}),
        "request id is not a UUID": body(actions(request_id="not-a-uuid")),
        "negative after_sequence": body(actions(after_sequence=-1)),
        "after_sequence above 2**31 - 1": body(actions(after_sequence=2**31)),
        "repository without owner": body(github(PULL_REQUEST, repository="repo")),
        "repository with a space": body(github(PULL_REQUEST, repository="owner/re po")),
        "repository ending in a newline": body(github(PULL_REQUEST, repository="owner/repo\n")),
        "repository above 200 characters": body(github(PULL_REQUEST, repository="o" * 100 + "/" + "r" * 100)),
        "unknown subject kind": body(github({"kind": "tag", "name": "v1"})),
        "pull request number 0": body(github({"kind": "pull_request", "number": 0})),
        "issue number below 0": body(github({"kind": "issue", "number": -1})),
        "empty branch name": body(github({"kind": "branch", "name": ""})),
        "branch name above 255 characters": body(github({"kind": "branch", "name": "b" * 256})),
        "uppercase commit SHA": body(github({"kind": "commit", "sha": "0123456789ABCDEF0123456789abcdef01234567"})),
        "short commit SHA": body(github({"kind": "commit", "sha": "0123456"})),
        "empty events": body(valid_pr | {"events": []}),
        "33 filters": body(github(COMMIT, events=[filter_("check_run", "a" * i) for i in range(1, 34)])),
        "unknown event": body(github(PULL_REQUEST, events=[filter_("fork")])),
        "check_run on an issue": body(github(ISSUE, events=[filter_("check_run")])),
        "issues on a pull request": body(github(PULL_REQUEST, events=[filter_("issues")])),
        "push on a pull request": body(github(PULL_REQUEST, events=[filter_("push")])),
        "pull_request on a branch": body(github(BRANCH, events=[filter_("pull_request")])),
        "actions on push": body(github(BRANCH, events=[filter_("push", "created")])),
        "actions on status": body(github(BRANCH, events=[filter_("status", "created")])),
        "empty actions": body(github(PULL_REQUEST, events=[{"event": "check_run", "actions": []}])),
        "uppercase action": body(github(PULL_REQUEST, events=[filter_("check_run", "Completed")])),
        "action with a digit": body(github(PULL_REQUEST, events=[filter_("check_run", "step1")])),
        "action above 64 characters": body(github(PULL_REQUEST, events=[filter_("check_run", "a" * 65)])),
        "33 actions": body(github(PULL_REQUEST, events=[filter_("check_run", *("a" * i for i in range(1, 34)))])),
        "extra filter field": body(github(PULL_REQUEST, events=[{"event": "status", "extra": 1}])),
    }


def comparisons() -> dict[str, tuple[Json, Json]]:
    filters = [filter_("check_run", "completed", "created"), filter_("status"), filter_("issue_comment")]
    return {
        "identical Action request": (body(actions()), body(actions())),
        "explicit defaults equal omitted ones": (body(actions()), body(actions(after_sequence=0), lifetime_days=7)),
        "another request": (body(actions()), body(actions(request_id=OTHER_REQUEST))),
        "another after_sequence": (body(actions()), body(actions(after_sequence=1))),
        "another lifetime": (body(actions()), body(actions(), lifetime_days=30)),
        "another session": (body(actions()), body(actions()) | {"session_id": "other-session"}),
        "another destination uid": (body(actions()), body(actions()) | {"destination_ref": destination(uid="other")}),
        "Action request against GitHub": (body(actions()), body(github(PULL_REQUEST))),
        "events in another order": (
            body(github(PULL_REQUEST, events=filters)),
            body(
                github(
                    PULL_REQUEST,
                    events=[filter_("issue_comment"), filter_("status"), filter_("check_run", "created", "completed")],
                )
            ),
        ),
        "repeated filters": (
            body(github(PULL_REQUEST, events=filters)),
            body(github(PULL_REQUEST, events=[*filters, filter_("status")])),
        ),
        "repeated actions": (
            body(github(PULL_REQUEST, events=[filter_("check_run", "completed")])),
            body(github(PULL_REQUEST, events=[filter_("check_run", "completed", "completed")])),
        ),
        "omitted events against null": (body(github(PULL_REQUEST)), body(github(PULL_REQUEST, events=None))),
        "omitted events against the default filters": (
            body(github(PULL_REQUEST)),
            body(
                github(
                    PULL_REQUEST,
                    events=[
                        filter_("status"),
                        filter_("check_run", "completed"),
                        filter_("pull_request"),
                        filter_("issue_comment"),
                        filter_("pull_request_review"),
                        filter_("pull_request_review_comment"),
                    ],
                )
            ),
        ),
        "any action against a listed one": (
            body(github(PULL_REQUEST, events=[filter_("check_run")])),
            body(github(PULL_REQUEST, events=[filter_("check_run", "completed")])),
        ),
        "another pull request": (body(github(PULL_REQUEST)), body(github({"kind": "pull_request", "number": 124}))),
        "pull request against issue of the same number": (
            body(github({"kind": "pull_request", "number": 7})),
            body(github(ISSUE)),
        ),
        "another repository": (body(github(PULL_REQUEST)), body(github(PULL_REQUEST, repository="test-owner/other"))),
    }


def events() -> dict[str, Json]:
    github_events = {
        f"GitHub {name.value}": GitHubEvent(
            provider="github", app_id=1_234_567, delivery_id=UUID(DELIVERY), repository_id=555_000_111, event=name
        )
        for name in EventName
    }
    return {
        "Action event": ActionsEvent(provider="actions", request_id=UUID(REQUEST), sequence=1).model_dump(mode="json"),
        "Action event, high sequence": ActionsEvent(
            provider="actions", request_id=UUID(OTHER_REQUEST), sequence=2**31 - 1
        ).model_dump(mode="json"),
        **{name: event.model_dump(mode="json") for name, event in github_events.items()},
        "GitHub event with an action": GitHubEvent(
            provider="github",
            app_id=1,
            delivery_id=UUID(DELIVERY),
            repository_id=2**40,
            event=EventName.CHECK_RUN,
            action="completed",
        ).model_dump(mode="json"),
    }


def notices() -> list[Json]:
    cases = {
        "nothing acknowledged": (0, 1),
        "partly acknowledged": (2, 3),
        "large cursors": (123_456_789, 2**40),
        "highest cursors": (2**62, 2**63 - 1),
    }
    return [
        {
            "name": name,
            "inbox_id": INBOX,
            "acknowledged": acknowledged,
            "through": through,
            "text": notice_text(UUID(INBOX), acknowledged=acknowledged, through=through),
        }
        for name, (acknowledged, through) in cases.items()
    ]


def runner_entries() -> list[Json]:
    """Entries as `Store.receipt` and `Store.checkpoint_before_attempt` serialize them into `notice.runner_entry`.
    A later claim compares those bytes with the entry History returns, serialized again, so both implementations
    must write the same bytes."""
    command = str(UUID(COMMAND))
    text = notice_text(UUID(INBOX), acknowledged=2, through=3)

    def entry(cursor: int, **event: object) -> event_log_pb2.EventEntry:
        return event_log_pb2.EventEntry(cursor=cursor, event=event_pb2.Event(**event))  # type: ignore[arg-type]

    reconciled = event_pb2.ConversationReconciled(
        turn_id="turn-1",
        items=[
            event_pb2.ItemRecovery(item_id="item-1", disposition=event_pb2.RECOVERY_DISPOSITION_RETAINED),
            event_pb2.ItemRecovery(
                item_id="item-2",
                disposition=event_pb2.RECOVERY_DISPOSITION_REVISED,
                replacement=event_pb2.RecoveredContent(text="revised", arguments_json='{"a":1}'),
            ),
        ],
    )
    entries = {
        "cursor only": event_log_pb2.EventEntry(cursor=1),
        "high cursor": event_log_pb2.EventEntry(cursor=2**40),
        "origin": event_log_pb2.EventEntry(
            cursor=7, origin=event_log_pb2.EventOrigin(source_id="runner-source", sequence=3)
        ),
        "event time and source sequences": event_log_pb2.EventEntry(
            cursor=9,
            event=event_pb2.Event(
                at=Timestamp(seconds=1_792_000_000, nanos=123_456_789),
                source_sequences=[1, 300, 2**40],
                harness_started=event_pb2.HarnessStarted(resumed=True, pid=4242),
            ),
        ),
        "command admitted with the notice text": entry(
            8,
            command_admitted=event_pb2.CommandAdmitted(
                command=command_pb2.Command(command_id=command, submit_input=command_pb2.SubmitInput(text=text))
            ),
        ),
        "command admitted with other text": entry(
            8,
            command_admitted=event_pb2.CommandAdmitted(
                command=command_pb2.Command(
                    command_id=command, submit_input=command_pb2.SubmitInput(text="unicode é 名 🚀 \n\u2028")
                )
            ),
        ),
        "command admitted for another command": entry(
            8,
            command_admitted=event_pb2.CommandAdmitted(
                command=command_pb2.Command(
                    command_id="another-command", change_model=command_pb2.ChangeModel(model="model-a")
                )
            ),
        ),
        "confirmation naming several commands": entry(
            11,
            harness_user_message_confirmed=event_pb2.HarnessUserMessageConfirmed(
                harness_message_id="message-1", text=text, origin_command_ids=["coalesced-other", command], turn_id="t"
            ),
        ),
        "command failed": entry(9, command_failed=event_pb2.CommandFailed(command_id=command, reason="refused")),
        "command no-op": entry(10, command_noop=event_pb2.CommandNoop(command_id=command, reason="nothing to do")),
        "empty message in a oneof": entry(12, harness_lost=event_pb2.HarnessLost()),
        "default values in a oneof": entry(13, harness_exited=event_pb2.HarnessExited()),
        "negative int32": entry(14, harness_exited=event_pb2.HarnessExited(exit_code=-1, stopped_by_runner=True)),
        "bytes": entry(15, setup_output=event_pb2.SetupOutput(stdout=b"\x00\xff\x80 text")),
        "enum and unicode": entry(
            16,
            turn_completed=event_pb2.TurnCompleted(
                turn_id="turn-1", status=event_pb2.TURN_STATUS_FAILED, error="boom é 名 🚀", ended_by_teardown=True
            ),
        ),
        "nested repeated messages": entry(17, conversation_reconciled=reconciled),
        "item completed with a tool result": entry(
            18,
            item_completed=event_pb2.ItemCompleted(
                item_id="item-1", tool=event_pb2.ToolResult(output="done", succeeded=True)
            ),
        ),
    }
    return [
        {
            "name": name,
            "entry": json_format.MessageToDict(value),
            "serialized": value.SerializeToString(deterministic=True).hex(),
        }
        for name, value in entries.items()
    ]


APP_ID = 1_234_567
WEBHOOK_SECRET = "test-webhook-secret-for-golden-vectors"
INSTALLATION_ID = 91_000_001
REPOSITORY_ID = 555_000_111
FORK_ID = 555_000_222
FORK = "test-fork-owner/test-repo"
HEAD_SHA = "0123456789abcdef0123456789abcdef01234567"
BASE_SHA = "89abcdef0123456789abcdef0123456789abcdef"
FORK_SHA = "fedcba9876543210fedcba9876543210fedcba98"
ZERO_SHA = "0" * 40


@dataclass(frozen=True)
class Webhook:
    """A delivery as GitHub sends it: the `X-GitHub-Event` header and the exact body bytes, as text."""

    event: str
    raw: str


def webhook(event: str, payload: object, *, separators: tuple[str, str] | None = None) -> Webhook:
    return Webhook(event=event, raw=json.dumps(payload, separators=separators))


def without(payload: Json, key: str) -> Json:
    return {k: v for k, v in payload.items() if k != key}


def person(login: str = "test-user", id_: int = 240_000_001) -> Json:
    return {
        "login": login,
        "id": id_,
        "node_id": f"U_test{id_}",
        "type": "User",
        "site_admin": False,
        "url": f"https://api.github.test/users/{login}",
    }


def repository(full_name: str = "test-owner/test-repo", id_: int = REPOSITORY_ID) -> Json:
    owner, name = full_name.split("/")
    return {
        "id": id_,
        "node_id": f"R_test{id_}",
        "name": name,
        "full_name": full_name,
        "private": True,
        "owner": person(owner),
        "html_url": f"https://github.test/{full_name}",
        "description": None,
        "fork": False,
        "created_at": 1_577_836_800,
        "updated_at": "2026-10-01T12:00:00Z",
        "pushed_at": 1_760_000_000,
        "size": 2_048,
        "stargazers_count": 0,
        "default_branch": "main",
        "topics": [],
        "license": None,
    }


def installation(**fields: object) -> Json:
    return {"id": INSTALLATION_ID, "node_id": "I_test91000001"} | fields


def envelope(action: str | None, **fields: object) -> Json:
    """The keys every repository event carries, then the event's own."""
    base: Json = {"installation": installation(), "repository": repository(), "sender": person()}
    return ({} if action is None else {"action": action}) | base | fields


def pull_reference(number: int) -> Json:
    """A pull request as a check or workflow run lists it: its repositories have no `full_name`."""
    repo = {"id": REPOSITORY_ID, "name": "test-repo", "url": "https://api.github.test/repos/test-owner/test-repo"}
    return {
        "id": 2_700_000_000 + number,
        "number": number,
        "url": f"https://api.github.test/repos/test-owner/test-repo/pulls/{number}",
        "head": {"ref": "feature/test-branch", "sha": HEAD_SHA, "repo": repo},
        "base": {"ref": "main", "sha": BASE_SHA, "repo": repo},
    }


def pull_request(head_repository: Json | None, number: int = 123, sha: str = HEAD_SHA) -> Json:
    return {
        "id": 2_700_000_000 + number,
        "node_id": f"PR_test{number}",
        "number": number,
        "state": "open",
        "title": "Test change",
        "user": person(),
        "body": None,
        "draft": False,
        "merged": False,
        "head": {
            "label": "test-owner:feature/test-branch",
            "ref": "feature/test-branch",
            "sha": sha,
            "repo": head_repository,
        },
        "base": {"label": "test-owner:main", "ref": "main", "sha": BASE_SHA, "repo": repository()},
        "commits": 3,
        "additions": 120,
        "deletions": 4,
        "changed_files": 5,
        "created_at": "2026-10-01T12:00:00Z",
        "updated_at": "2026-10-02T08:30:00Z",
    }


def issue(number: int = 7, **fields: object) -> Json:
    return {
        "id": 3_900_000_000 + number,
        "node_id": f"I_test{number}",
        "number": number,
        "title": "Test issue",
        "user": person(),
        "state": "open",
        "labels": [],
        "comments": 2,
        "created_at": "2026-10-01T12:00:00Z",
        "body": "kept verbatim",
    } | fields


def comment(**fields: object) -> Json:
    return {
        "id": 3_500_000_123,
        "node_id": "IC_test",
        "user": person(),
        "body": "kept verbatim",
        "created_at": "2026-10-02T08:30:00Z",
    } | fields


def check_run(sha: str = HEAD_SHA, pull_requests: list[Json] | None = None) -> Json:
    return {
        "id": 31_000_000_123,
        "node_id": "CR_test",
        "head_sha": sha,
        "external_id": "",
        "name": "test-check",
        "status": "completed",
        "conclusion": "success",
        "started_at": "2026-10-02T08:00:00Z",
        "completed_at": "2026-10-02T08:05:00Z",
        "output": {"title": None, "summary": None, "text": None, "annotations_count": 0},
        "check_suite": {"id": 28_000_000_001},
        "app": {"id": 1_000_001, "slug": "test-ci", "name": "Test CI"},
        "pull_requests": [pull_reference(123)] if pull_requests is None else pull_requests,
    }


def check_suite(pull_requests: list[Json]) -> Json:
    return {
        "id": 28_000_000_001,
        "node_id": "CS_test",
        "head_branch": "feature/test-branch",
        "head_sha": HEAD_SHA,
        "status": "completed",
        "conclusion": "success",
        "app": {"id": 1_000_001, "slug": "test-ci"},
        "pull_requests": pull_requests,
    }


def workflow_run(head_branch: str | None, pull_requests: list[Json]) -> Json:
    return {
        "id": 27_000_000_001,
        "name": "test-workflow",
        "head_branch": head_branch,
        "head_sha": HEAD_SHA,
        "run_number": 412,
        "run_attempt": 1,
        "event": "push",
        "status": "completed",
        "conclusion": "success",
        "workflow_id": 55_001,
        "pull_requests": pull_requests,
    }


def workflow_job(head_branch: str | None) -> Json:
    return {
        "id": 27_000_000_002,
        "run_id": 27_000_000_001,
        "head_sha": HEAD_SHA,
        "head_branch": head_branch,
        "name": "test-job",
        "status": "completed",
        "conclusion": "success",
        "labels": ["test-runner"],
        "steps": [{"name": "Set up job", "status": "completed", "conclusion": "success", "number": 1}],
    }


def status(branches: list[str]) -> Json:
    return envelope(
        None,
        id=9_000_000_001,
        sha=HEAD_SHA,
        name="test-owner/test-repo",
        state="success",
        context="ci/test",
        branches=[{"name": name, "commit": {"sha": HEAD_SHA}, "protected": False} for name in branches],
        created_at="2026-10-02T08:05:00Z",
    )


def push(ref: str, after: str = HEAD_SHA) -> Json:
    return envelope(
        None,
        ref=ref,
        before=BASE_SHA,
        after=after,
        created=False,
        deleted=after == ZERO_SHA,
        forced=False,
        commits=[{"id": HEAD_SHA, "message": "Test commit", "timestamp": "2026-10-02T08:00:00Z"}],
        pusher={"name": "test-user"},
    )


def installation_event(action: str, **fields: object) -> Json:
    return {
        "action": action,
        "installation": installation(
            app_id=APP_ID,
            target_type="Organization",
            permissions={"checks": "read", "pull_requests": "read"},
            suspended_at=None,
            created_at="2026-09-01T00:00:00Z",
        ),
        "sender": person(),
    } | fields


def accepted_webhooks() -> dict[str, Webhook]:
    own, fork = repository(), repository(FORK, FORK_ID)
    two_pulls = [pull_reference(123), pull_reference(124)]
    pull_marker = {"url": "https://api.github.test/repos/test-owner/test-repo/pulls/123"}
    return {
        "pull request from the same repository": webhook(
            "pull_request", envelope("opened", pull_request=pull_request(own))
        ),
        "pull request from a fork": webhook(
            "pull_request", envelope("synchronize", pull_request=pull_request(fork, sha=FORK_SHA))
        ),
        "pull request whose fork is deleted": webhook(
            "pull_request", envelope("closed", pull_request=pull_request(None))
        ),
        "pull request review": webhook(
            "pull_request_review", envelope("submitted", pull_request=pull_request(own), review={"id": 3_100_000_001})
        ),
        "pull request review comment": webhook(
            "pull_request_review_comment", envelope("created", pull_request=pull_request(fork), comment=comment())
        ),
        "comment on a pull request": webhook(
            "issue_comment", envelope("created", issue=issue(123, pull_request=pull_marker), comment=comment())
        ),
        "comment on an issue": webhook("issue_comment", envelope("created", issue=issue(), comment=comment())),
        "comment on an issue with a null pull request": webhook(
            "issue_comment", envelope("edited", issue=issue(pull_request=None), comment=comment())
        ),
        # `is not None`, not truthiness: an empty object still marks a pull request.
        "comment on an issue with an empty pull request": webhook(
            "issue_comment", envelope("created", issue=issue(pull_request={}), comment=comment())
        ),
        "issue opened": webhook("issues", envelope("opened", issue=issue())),
        "check run with two pull requests": webhook(
            "check_run", envelope("completed", check_run=check_run(pull_requests=two_pulls))
        ),
        "check run listing a pull request twice": webhook(
            "check_run",
            envelope("completed", check_run=check_run(pull_requests=[pull_reference(123), pull_reference(123)])),
        ),
        "check run without pull requests": webhook(
            "check_run", envelope("created", check_run=check_run(pull_requests=[]))
        ),
        "check suite": webhook("check_suite", envelope("completed", check_suite=check_suite(two_pulls))),
        "status on two branches": webhook("status", status(["main", "feature/test-branch"])),
        "status on no branch": webhook("status", status([])),
        "workflow run with pull requests and a branch": webhook(
            "workflow_run", envelope("completed", workflow_run=workflow_run("feature/test-branch", two_pulls))
        ),
        "workflow run without a branch": webhook(
            "workflow_run", envelope("requested", workflow_run=workflow_run(None, []))
        ),
        "workflow job on a branch": webhook(
            "workflow_job", envelope("completed", workflow_job=workflow_job("feature/ü-🚀"))
        ),
        "workflow job without a branch": webhook("workflow_job", envelope("queued", workflow_job=workflow_job(None))),
        "push to a branch": webhook("push", push("refs/heads/feature/test-branch")),
        "push that deletes a branch": webhook("push", push("refs/heads/feature/test-branch", after=ZERO_SHA)),
        "push to a tag": webhook("push", push("refs/tags/v1.0.0")),
        "push to the longest branch name": webhook("push", push("refs/heads/" + "b" * 255)),
        "branch created": webhook(
            "create", envelope(None, ref="feature/test-branch", ref_type="branch", master_branch="main")
        ),
        "tag created": webhook("create", envelope(None, ref="v1.0.0", ref_type="tag", master_branch="main")),
        "branch deleted": webhook("delete", envelope(None, ref="feature/test-branch", ref_type="branch")),
        "tag deleted": webhook("delete", envelope(None, ref="v1.0.0", ref_type="tag")),
        "installation created": webhook(
            "installation", installation_event("created", repositories=[{"id": REPOSITORY_ID, "name": "test-repo"}])
        ),
        "installation suspended": webhook(
            "installation",
            installation_event("suspend")
            | {"installation": installation(app_id=APP_ID, suspended_at="2026-10-02T03:04:05Z")},
        ),
        "installation repositories added": webhook(
            "installation_repositories",
            installation_event("added", repository_selection="selected", repositories_added=[{"id": REPOSITORY_ID}]),
        ),
        "installation event that names a repository": webhook(
            "installation", installation_event("new_permissions_accepted", repository=repository())
        ),
        "no action": webhook("check_run", without(envelope("completed", check_run=check_run()), "action")),
        "null action": webhook("check_run", envelope(None, check_run=check_run()) | {"action": None}),
        "longest action": webhook("check_run", envelope("a" * 64, check_run=check_run())),
        "multibyte action at the length limit": webhook("check_run", envelope("é" * 64, check_run=check_run())),
        "pretty-printed non-ASCII body with a trailing newline": Webhook(
            event="issue_comment",
            raw=json.dumps(
                envelope("created", issue=issue(), comment=comment(body='ünïcode 🚀 \u2028 "quoted" \\ back')),
                indent=2,
                ensure_ascii=False,
            )
            + "\n",
        ),
        "compact body": webhook(
            "issue_comment", envelope("created", issue=issue(), comment=comment()), separators=(",", ":")
        ),
        "repository id beyond 32 bits": webhook(
            "issues", envelope("opened", issue=issue(), repository=repository(id_=2**40 + 5))
        ),
        # A double holds an integer exactly only up to 2**53; the payload is kept as JSON, so these must survive.
        "integers beyond 2**53": webhook(
            "issue_comment",
            envelope("created", issue=issue(), comment=comment(id=2**53 + 1, reactions={"total_count": 2**63 - 1})),
        ),
    }


def rejected_webhooks() -> dict[str, Webhook]:
    valid = envelope("opened", issue=issue())
    branch_push = push("refs/heads/feature/test-branch")
    return {
        "unknown event": webhook("fork", valid),
        "empty event": webhook("", valid),
        "event the service does not take": webhook("pull_request_review_thread", valid),
        "check run body under the check suite event": webhook(
            "check_suite", envelope("completed", check_run=check_run())
        ),
        "push body under the create event": webhook("create", branch_push),
        "no repository on an issue event": webhook("issues", without(valid, "repository")),
        "no repository on a push": webhook("push", without(branch_push, "repository")),
        "null repository on a check run": webhook(
            "check_run", envelope("completed", check_run=check_run()) | {"repository": None}
        ),
        "no installation": webhook("issues", without(valid, "installation")),
        "installation id 0": webhook("issues", valid | {"installation": installation(id=0)}),
        "negative installation id": webhook("issues", valid | {"installation": installation(id=-5)}),
        "null installation": webhook("issues", valid | {"installation": None}),
        "installation id as a word": webhook("issues", valid | {"installation": installation(id="eleven")}),
        "unparseable suspended_at": webhook("issues", valid | {"installation": installation(suspended_at="yesterday")}),
        "repository id 0": webhook("issues", valid | {"repository": repository(id_=0)}),
        "repository without a name": webhook("issues", valid | {"repository": without(repository(), "full_name")}),
        "repository name without an owner": webhook(
            "issues", valid | {"repository": repository() | {"full_name": "repo"}}
        ),
        "repository name with a space": webhook(
            "issues", valid | {"repository": repository() | {"full_name": "owner/re po"}}
        ),
        "repository name ending in a newline": webhook(
            "issues", valid | {"repository": repository() | {"full_name": "owner/repo\n"}}
        ),
        "repository name above 200 characters": webhook(
            "issues", valid | {"repository": repository() | {"full_name": "o" * 100 + "/" + "r" * 100}}
        ),
        "action above 64 characters": webhook("issues", valid | {"action": "a" * 65}),
        "multibyte action above 64 characters": webhook("issues", valid | {"action": "é" * 65}),
        "action as a number": webhook("issues", valid | {"action": 5}),
        "body is a list": Webhook(event="issues", raw="[]"),
        "body is a string": Webhook(event="issues", raw='"text"'),
        "body is null": Webhook(event="issues", raw="null"),
        "body is a number": Webhook(event="issues", raw="12"),
        "body is not JSON": Webhook(event="issues", raw="{invalid"),
        "empty body": Webhook(event="issues", raw=""),
        "no issue": webhook("issues", without(valid, "issue")),
        "issue without a number": webhook("issues", valid | {"issue": without(issue(), "number")}),
        "issue number 0": webhook("issues", valid | {"issue": issue(0)}),
        "negative issue number": webhook("issue_comment", valid | {"issue": issue(-1)}),
        "pull request marker that is not an object": webhook(
            "issue_comment", valid | {"issue": issue(pull_request="https://api.github.test/pulls/7")}
        ),
        "no pull request": webhook("pull_request", envelope("opened")),
        "pull request without a head": webhook(
            "pull_request", envelope("opened", pull_request=without(pull_request(None), "head"))
        ),
        "pull request head without a SHA": webhook(
            "pull_request",
            envelope("opened", pull_request=pull_request(None) | {"head": {"ref": "feature/test-branch"}}),
        ),
        "pull request head with an invalid repository": webhook(
            "pull_request", envelope("opened", pull_request=pull_request(repository(id_=0)))
        ),
        "pull request number 0": webhook("pull_request", envelope("opened", pull_request=pull_request(None, number=0))),
        "check run without a SHA": webhook(
            "check_run", envelope("completed", check_run=without(check_run(), "head_sha"))
        ),
        "check run pull request without a number": webhook(
            "check_run",
            envelope("completed", check_run=check_run(pull_requests=[without(pull_reference(1), "number")])),
        ),
        "check run pull request number 0": webhook(
            "check_run", envelope("completed", check_run=check_run(pull_requests=[pull_reference(0)]))
        ),
        "check run with a null pull request list": webhook(
            "check_run", envelope("completed", check_run=check_run() | {"pull_requests": None})
        ),
        "workflow job without a SHA": webhook(
            "workflow_job", envelope("completed", workflow_job=without(workflow_job(None), "head_sha"))
        ),
        "workflow job on an empty branch name": webhook(
            "workflow_job", envelope("completed", workflow_job=workflow_job(""))
        ),
        "workflow run on a branch name above 255 characters": webhook(
            "workflow_run", envelope("completed", workflow_run=workflow_run("b" * 256, []))
        ),
        "status without a SHA": webhook("status", without(status([]), "sha")),
        "status on an empty branch name": webhook("status", status([""])),
        "push without a ref": webhook("push", without(branch_push, "ref")),
        "push without an after SHA": webhook("push", without(branch_push, "after")),
        "push to an empty branch name": webhook("push", push("refs/heads/")),
        "push to a branch name above 255 characters": webhook("push", push("refs/heads/" + "b" * 256)),
        "create without a ref type": webhook("create", envelope(None, ref="feature/test-branch")),
        "create of another ref type": webhook("create", envelope(None, ref="feature/test-branch", ref_type="commit")),
        "create of an empty branch name": webhook("create", envelope(None, ref="", ref_type="branch")),
        "delete without a ref": webhook("delete", envelope(None, ref_type="branch")),
    }


class RecordingStore(Store):
    """What `GitHub.ingest` hands the store, instead of a database: the call that writes the delivery."""

    def __init__(self) -> None:
        self.call: Json | None = None

    async def ingest_github(
        self,
        app_id: int,
        delivery_id: UUID,
        installation_id: int,
        repository_id: int | None,
        event: str,
        digest: bytes,
        payload: dict[str, JsonValue],
        *,
        action: str | None,
        head_sha: str | None,
        subjects: Sequence[SubjectKey],
        repository_name: str | None = None,
        revisions: Sequence[HeadRevision] = (),
    ) -> bool:
        self.call = {
            "app_id": app_id,
            "delivery_id": str(delivery_id),
            "installation_id": installation_id,
            "repository_id": repository_id,
            "event": event,
            "digest": digest.hex(),
            "payload": payload,
            "action": action,
            "head_sha": head_sha,
            "subjects": [dataclasses.asdict(key) for key in subjects],
            "repository_name": repository_name,
            "revisions": [dataclasses.asdict(revision) for revision in revisions],
        }
        return True


def delivery_id(name: str) -> UUID:
    return uuid5(UUID(DELIVERY), name)


async def ingest(github: GitHub, hook: Webhook, name: str) -> Json | str:
    """The call `GitHub.ingest` makes to write the delivery, or why it refuses the delivery."""
    store = RecordingStore()
    raw = hook.raw.encode()
    signature = "sha256=" + hmac.new(WEBHOOK_SECRET.encode(), raw, hashlib.sha256).hexdigest()
    try:
        await github.ingest(store, hook.event, delivery_id(name), signature, raw)
    except ValueError as error:
        assert store.call is None
        match error:
            case UnsupportedWebhookEventError():
                return "unsupported_event"
            case MissingWebhookRepositoryError():
                return "missing_repository"
            case ValidationError():
                return "payload_validation"
        raise
    assert store.call is not None
    return store.call


async def webhook_vectors() -> tuple[list[Json], list[Json]]:
    settings = GitHubSettings(
        app_id=APP_ID, private_key=SecretStr("unused-by-the-vectors"), webhook_secret=SecretStr(WEBHOOK_SECRET)
    )
    accepted: list[Json] = []
    rejected: list[Json] = []
    async with httpx.AsyncClient() as http:
        github = GitHub(GitHubClient(http, settings))
        for name, hook in accepted_webhooks().items():
            result = await ingest(github, hook, name)
            assert isinstance(result, dict), f"{name} is refused as {result}"
            accepted.append(
                {
                    "name": name,
                    "event": hook.event,
                    "delivery_id": str(delivery_id(name)),
                    "raw": hook.raw,
                    "ingest": result,
                }
            )
        for name, hook in rejected_webhooks().items():
            result = await ingest(github, hook, name)
            assert isinstance(result, str), f"{name} is accepted"
            rejected.append(
                {
                    "name": name,
                    "event": hook.event,
                    "delivery_id": str(delivery_id(name)),
                    "raw": hook.raw,
                    "reason": result,
                }
            )
    return accepted, rejected


def subject_keys() -> list[Json]:
    subjects: list[Subject] = [
        PullRequestSubject(kind="pull_request", number=123),
        PullRequestSubject(kind="pull_request", number=2**40),
        IssueSubject(kind="issue", number=7),
        BranchSubject(kind="branch", name="feature/test-branch"),
        BranchSubject(kind="branch", name="feature/ü-🚀"),
        BranchSubject(kind="branch", name=" padded "),
        BranchSubject(kind="branch", name="b" * 255),
        CommitSubject(kind="commit", sha=HEAD_SHA),
    ]
    return [
        {"subject": subject.model_dump(mode="json"), "kind": subject.kind, "key": subject_key(subject)}
        for subject in subjects
    ]


async def build() -> Json:
    valid = valid_subscriptions()
    invalid = []
    for name, case in invalid_subscriptions().items():
        try:
            Subscribe.model_validate(case)
        except ValidationError:
            invalid.append({"name": name, "input": case})
        else:
            raise AssertionError(f"{name} is valid")
    deliveries, rejected_deliveries = await webhook_vectors()
    return {
        "destination_keys": destination_keys(),
        "subscriptions": [
            {"name": name, "input": case, "stored": stored(Subscribe.model_validate(case))}
            for name, case in valid.items()
        ],
        "invalid_subscriptions": invalid,
        "comparisons": [
            {
                "name": name,
                "a": a,
                "b": b,
                # The comparison `Store.subscribe` makes: the stored creation against the new body.
                "same": round_trip(Subscribe.model_validate(a)) == Subscribe.model_validate(b),
            }
            for name, (a, b) in comparisons().items()
        ],
        "events": [{"name": name, "event": event} for name, event in events().items()],
        "notices": notices(),
        "command_id": {"input": COMMAND, "written": str(UUID(COMMAND))},
        "runner_entries": runner_entries(),
        "deliveries": deliveries,
        "rejected_deliveries": rejected_deliveries,
        "subject_keys": subject_keys(),
    }


def serialize(golden: Json) -> str:
    return json.dumps(golden, indent=1, sort_keys=True) + "\n"


async def async_main(path: Path) -> None:
    await asyncio.to_thread(path.write_text, serialize(await build()), encoding="utf-8")


def main() -> None:
    asyncio.run(async_main(Path(sys.argv[1])))


if __name__ == "__main__":
    main()
