"""Golden vectors that pin the Go store (`agentplane/notification_service/store`) to the stored forms of this one.

Stored rows hold values this service computed or serialized: the inbox's `destination_key`, a subscription's
`creation` JSON, an entry's `event` JSON, a notice's `text` and the `EventEntry` bytes of `notice.runner_entry`.
Each vector is produced by the Python code that writes the column, and the Go store must reproduce it and read
it back. A `creation` the Python model rejects is a vector too, as is whether two `creation` values name the
same subscription.

Regenerate with `bb run //agentplane/notification_service:generate_golden_bin -- <path to golden.json>`.
"""

import json
import sys
from pathlib import Path
from uuid import UUID

from google.protobuf import json_format
from google.protobuf.timestamp_pb2 import Timestamp
from pydantic import ValidationError

from agentplane.notification_service.db import Subscription
from agentplane.notification_service.models import ActionsEvent, DestinationRef, Subscribe
from agentplane.notification_service.sources.github.models import EventName, GitHubEvent
from agentplane.notification_service.store import destination_key, notice_text
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


def build() -> Json:
    valid = valid_subscriptions()
    invalid = []
    for name, case in invalid_subscriptions().items():
        try:
            Subscribe.model_validate(case)
        except ValidationError:
            invalid.append({"name": name, "input": case})
        else:
            raise AssertionError(f"{name} is valid")
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
    }


def serialize(golden: Json) -> str:
    return json.dumps(golden, indent=1, sort_keys=True) + "\n"


def main() -> None:
    Path(sys.argv[1]).write_text(serialize(build()), encoding="utf-8")


if __name__ == "__main__":
    main()
