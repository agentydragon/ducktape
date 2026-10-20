import asyncio
import json
from pathlib import Path
from uuid import UUID

import grpc
import pytest
import pytest_bazel
from google.protobuf.empty_pb2 import Empty
from google.protobuf.timestamp_pb2 import Timestamp

from agentplane.app.notifications_operator_client import NotificationsOperatorClient
from agentplane.app.testing.notifications_operator import FakeNotificationsOperator
from agentplane.grpc.calls import ServiceError
from agentplane.notification_service import notifications_operator_pb2 as pb, notifications_pb2

SANDBOX_UID = UUID("00000000-0000-4000-8000-0000000000a1")
SESSION = UUID("00000000-0000-4000-8000-0000000000b1")
OBSERVED = Timestamp()
OBSERVED.FromJsonString("2026-09-21T12:00:00Z")


def sandbox_snapshot(*inboxes: pb.InboxStatus) -> pb.WatchSandboxNotificationsResponse:
    return pb.WatchSandboxNotificationsResponse(
        snapshot=pb.SandboxNotificationStatus(observed_at=OBSERVED, inboxes=inboxes)
    )


def parse(frame: bytes) -> tuple[str, dict[str, object]]:
    event, data = frame.decode().removesuffix("\n\n").split("\n")
    return event.removeprefix("event: "), json.loads(data.removeprefix("data: "))


async def test_sandbox_snapshots_arrive_as_proto_json_frames_until_planned_renewal(
    notifications: NotificationsOperatorClient, notifications_operator: FakeNotificationsOperator
) -> None:
    inbox = pb.InboxStatus(
        inbox=notifications_pb2.Inbox(id="inbox-a", session_id=str(SESSION), last_cursor=7),
        unannounced_count=2,
        pending_entries=[pb.InboxEntrySummary(cursor=7, provider="github", summary="CI failed")],
        notice_wait_reason=pb.NOTICE_WAIT_REASON_DEBOUNCING,
    )
    notifications_operator.sandbox_script.put_nowait(sandbox_snapshot(inbox))
    notifications_operator.sandbox_script.put_nowait(sandbox_snapshot())
    notifications_operator.sandbox_script.put_nowait(pb.WatchSandboxNotificationsResponse(reconnect_required=Empty()))

    frames = [
        parse(frame)
        async for frame in notifications.watch_sandbox_notifications(
            namespace="test-namespace", name="test-sandbox", uid=SANDBOX_UID
        )
    ]

    assert [event for event, _ in frames] == ["snapshot", "snapshot"]
    (_, first), (_, empty) = frames
    assert first["inboxes"] == [
        {
            "inbox": {
                "id": "inbox-a",
                "sessionId": str(SESSION),
                "lastCursor": "7",
                "acknowledged": "0",
                "covered": "0",
                "expiredThrough": "0",
                "retired": False,
            },
            "subscriptions": [],
            "unannouncedCount": 2,
            "pendingAcknowledgementCount": 0,
            "pendingEntries": [{"cursor": "7", "provider": "github", "summary": "CI failed"}],
            "pendingEntriesMore": False,
            "noticeWaitReason": "NOTICE_WAIT_REASON_DEBOUNCING",
        }
    ]
    assert empty == {"observedAt": "2026-09-21T12:00:00Z", "inboxes": []}
    assert notifications_operator.sandbox_requests == [
        pb.WatchSandboxNotificationsRequest(namespace="test-namespace", name="test-sandbox", uid=str(SANDBOX_UID))
    ]


async def test_session_notice_snapshots_name_the_sessions(
    notifications: NotificationsOperatorClient, notifications_operator: FakeNotificationsOperator
) -> None:
    state = pb.InboxNoticeState(
        inbox_id="inbox-a", session_id=str(SESSION), queued=1, notice=pb.NOTICE_PROGRESS_ADMITTED
    )
    notifications_operator.session_script.put_nowait(
        pb.WatchSessionNoticesResponse(snapshot=pb.SessionNoticeStates(observed_at=OBSERVED, inboxes=[state]))
    )
    notifications_operator.session_script.put_nowait(pb.WatchSessionNoticesResponse(reconnect_required=Empty()))

    ((event, data),) = [parse(frame) async for frame in notifications.watch_session_notices([SESSION])]

    assert event == "snapshot"
    assert data["inboxes"] == [
        {
            "inboxId": "inbox-a",
            "sessionId": str(SESSION),
            "queued": 1,
            "unacknowledged": 0,
            "notice": "NOTICE_PROGRESS_ADMITTED",
        }
    ]
    assert [list(request.session_ids) for request in notifications_operator.session_requests] == [[str(SESSION)]]


async def test_a_watch_ended_without_planned_renewal_fails(
    notifications: NotificationsOperatorClient, notifications_operator: FakeNotificationsOperator
) -> None:
    notifications_operator.sandbox_script.put_nowait(sandbox_snapshot())
    notifications_operator.sandbox_script.put_nowait(None)
    frames = notifications.watch_sandbox_notifications(namespace="test-namespace", name="test-sandbox", uid=SANDBOX_UID)

    assert parse(await anext(frames))[0] == "snapshot"
    with pytest.raises(ConnectionError):
        await anext(frames)


@pytest.mark.parametrize("code", [grpc.StatusCode.NOT_FOUND, grpc.StatusCode.UNAVAILABLE])
async def test_a_failed_watch_raises_with_the_status_code(
    notifications: NotificationsOperatorClient, notifications_operator: FakeNotificationsOperator, code: grpc.StatusCode
) -> None:
    notifications_operator.session_script.put_nowait(code)

    with pytest.raises(ServiceError) as raised:
        await anext(notifications.watch_session_notices([SESSION]))

    assert raised.value.code == code


async def test_a_service_that_does_not_open_in_time_fails(
    notifications_channel: grpc.aio.Channel, notifications_token_file: Path
) -> None:
    impatient = NotificationsOperatorClient(
        notifications_channel, token_file=notifications_token_file, request_timeout_s=0.1
    )

    with pytest.raises(TimeoutError):
        await anext(impatient.watch_session_notices([SESSION]))


async def test_each_watch_reads_the_rotating_token_afresh(
    notifications: NotificationsOperatorClient,
    notifications_operator: FakeNotificationsOperator,
    notifications_token_file: Path,
) -> None:
    for token in ("token-one", "token-two"):
        await asyncio.to_thread(notifications_token_file.write_text, f"{token}\n")
        notifications_operator.session_script.put_nowait(pb.WatchSessionNoticesResponse(reconnect_required=Empty()))
        assert [frame async for frame in notifications.watch_session_notices([SESSION])] == []

    assert notifications_operator.tokens == ["Bearer token-one", "Bearer token-two"]


async def test_a_consumer_that_stops_hangs_up_on_the_watch(
    notifications: NotificationsOperatorClient, notifications_operator: FakeNotificationsOperator
) -> None:
    notifications_operator.session_script.put_nowait(
        pb.WatchSessionNoticesResponse(snapshot=pb.SessionNoticeStates(observed_at=OBSERVED))
    )
    frames = notifications.watch_session_notices([SESSION])
    await anext(frames)

    await frames.aclose()

    async with asyncio.timeout(10):
        await notifications_operator.hung_up.wait()


if __name__ == "__main__":
    pytest_bazel.main()
