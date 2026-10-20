package api

import (
	"io"
	"testing"
	"time"

	operatorpb "github.com/agentydragon/ducktape/agentplane/notification_service/notifications_operatorpb"
	"github.com/agentydragon/ducktape/agentplane/notification_service/notificationspb"
	"github.com/agentydragon/ducktape/agentplane/notification_service/store"
	"google.golang.org/grpc"
)

type sandboxWatch = grpc.ServerStreamingClient[operatorpb.WatchSandboxNotificationsResponse]

type noticeWatch = grpc.ServerStreamingClient[operatorpb.WatchSessionNoticesResponse]

func (s testServer) watchSandbox(t *testing.T) sandboxWatch {
	t.Helper()
	stream, err := s.operator.WatchSandboxNotifications(bearing(t, operatorToken), sandboxWatchRequest(sandboxUID))
	if err != nil {
		t.Fatal(err)
	}
	return stream
}

func (s testServer) watchNotices(t *testing.T, sessionIDs ...string) noticeWatch {
	t.Helper()
	stream, err := s.operator.WatchSessionNotices(bearing(t, operatorToken), &operatorpb.WatchSessionNoticesRequest{SessionIds: sessionIDs})
	if err != nil {
		t.Fatal(err)
	}
	return stream
}

// untilSandbox receives snapshots until one shows what the condition wants. A commit that changes several rows
// can be shown in steps, so a test waits for the state it expects rather than for a count of frames.
func untilSandbox(t *testing.T, stream sandboxWatch, wanted func(*operatorpb.SandboxNotificationStatus) bool) *operatorpb.SandboxNotificationStatus {
	t.Helper()
	for {
		response, err := stream.Recv()
		if err != nil {
			t.Fatalf("the watch ended before it showed the state: %v", err)
		}
		if response.GetSnapshot() == nil {
			t.Fatalf("want a snapshot, received %v", response)
		}
		if wanted(response.GetSnapshot()) {
			return response.GetSnapshot()
		}
	}
}

func untilNotices(t *testing.T, stream noticeWatch, wanted func(*operatorpb.SessionNoticeStates) bool) *operatorpb.SessionNoticeStates {
	t.Helper()
	for {
		response, err := stream.Recv()
		if err != nil {
			t.Fatalf("the watch ended before it showed the state: %v", err)
		}
		if response.GetSnapshot() == nil {
			t.Fatalf("want a snapshot, received %v", response)
		}
		if wanted(response.GetSnapshot()) {
			return response.GetSnapshot()
		}
	}
}

func wantSandboxReconnectAndCleanEnd(t *testing.T, stream sandboxWatch) {
	t.Helper()
	if response, err := stream.Recv(); err != nil || response.GetReconnectRequired() == nil {
		t.Fatalf("want reconnect_required, received %v, %v", response, err)
	}
	if _, err := stream.Recv(); err != io.EOF {
		t.Fatalf("want the watch to end cleanly, received %v", err)
	}
}

func wantNoticesReconnectAndCleanEnd(t *testing.T, stream noticeWatch) {
	t.Helper()
	if response, err := stream.Recv(); err != nil || response.GetReconnectRequired() == nil {
		t.Fatalf("want reconnect_required, received %v, %v", response, err)
	}
	if _, err := stream.Recv(); err != io.EOF {
		t.Fatalf("want the watch to end cleanly, received %v", err)
	}
}

func TestSandboxWatchOpensWithASnapshotAndFollowsCommittedChanges(t *testing.T) {
	server := newServer(t)
	stream := server.watchSandbox(t)
	opened := untilSandbox(t, stream, func(*operatorpb.SandboxNotificationStatus) bool { return true })
	if len(opened.GetInboxes()) != 0 || opened.GetObservedAt() == nil {
		t.Fatalf("opening snapshot = %v", opened)
	}

	subscription := server.subscribe(t, "stream", "stream-session")
	subscribed := untilSandbox(t, stream, func(s *operatorpb.SandboxNotificationStatus) bool { return len(s.GetInboxes()) == 1 })
	if inbox := subscribed.GetInboxes()[0]; inbox.GetInbox().GetId() != subscription.GetInboxId() || len(inbox.GetSubscriptions()) != 1 {
		t.Errorf("subscribed = %v", subscribed)
	}

	server.record(t, subscription, 1)
	untilSandbox(t, stream, func(s *operatorpb.SandboxNotificationStatus) bool {
		return s.GetInboxes()[0].GetPendingAcknowledgementCount() == 1
	})

	if _, err := server.client.CancelSubscription(bearing(t, sandboxToken), &notificationspb.CancelSubscriptionRequest{SubscriptionId: subscription.GetId()}); err != nil {
		t.Fatal(err)
	}
	untilSandbox(t, stream, func(s *operatorpb.SandboxNotificationStatus) bool {
		return s.GetInboxes()[0].GetSubscriptions()[0].GetCancelled()
	})

	if _, err := server.client.RetireInbox(bearing(t, sandboxToken), &notificationspb.RetireInboxRequest{InboxId: subscription.GetInboxId()}); err != nil {
		t.Fatal(err)
	}
	retired := untilSandbox(t, stream, func(s *operatorpb.SandboxNotificationStatus) bool { return s.GetInboxes()[0].GetInbox().GetRetired() })
	if reason := retired.GetInboxes()[0].GetNoticeWaitReason(); reason != operatorpb.NoticeWaitReason_NOTICE_WAIT_REASON_RETIRED {
		t.Errorf("a retired inbox waits for %v", reason)
	}
}

func TestAWakeupThatChangesNothingSendsNoSnapshot(t *testing.T) {
	server := newServer(t, func(c *serverConfig) { c.lease = 2 * time.Second })
	stream := server.watchSandbox(t)
	untilSandbox(t, stream, func(*operatorpb.SandboxNotificationStatus) bool { return true })
	server.exec(t, `SELECT pg_notify($1, '')`, store.WorkChannel)
	// The wakeup read the same state again, so what follows is the end of the lease and not another snapshot.
	wantSandboxReconnectAndCleanEnd(t, stream)

	notices := server.watchNotices(t, testSession)
	untilNotices(t, notices, func(*operatorpb.SessionNoticeStates) bool { return true })
	server.exec(t, `SELECT pg_notify($1, '')`, store.WorkChannel)
	wantNoticesReconnectAndCleanEnd(t, notices)
}

func TestSandboxWatchReadsAgainWhenADebounceFallsDue(t *testing.T) {
	server := newServer(t, func(c *serverConfig) { c.debounce = store.Debounce{Quiet: 2 * time.Second, MaxWait: 4 * time.Second} })
	subscription := server.subscribe(t, "due")
	server.record(t, subscription, 1)
	stream := server.watchSandbox(t)
	debouncing := untilSandbox(t, stream, func(*operatorpb.SandboxNotificationStatus) bool { return true })
	if inbox := debouncing.GetInboxes()[0]; inbox.GetNoticeWaitReason() != operatorpb.NoticeWaitReason_NOTICE_WAIT_REASON_DEBOUNCING || inbox.GetNoticeDueAt() == nil {
		t.Fatalf("the opening snapshot should show the entry debouncing: %v", debouncing)
	}
	// Nothing is committed from here on: the notice falls due with the clock.
	due := untilSandbox(t, stream, func(s *operatorpb.SandboxNotificationStatus) bool {
		return s.GetInboxes()[0].GetNoticeWaitReason() == operatorpb.NoticeWaitReason_NOTICE_WAIT_REASON_READY
	})
	if inbox := due.GetInboxes()[0]; inbox.GetNoticeDueAt() != nil || inbox.GetUnannouncedCount() != 1 {
		t.Errorf("a notice that is due shows %v", inbox)
	}
}

func TestSandboxWatchReadsAgainWhenASubscriptionExpires(t *testing.T) {
	server := newServer(t)
	subscription := server.subscribe(t, "expires")
	server.exec(t, `UPDATE subscription SET expires_at = $2 WHERE id = $1`, subscription.GetId(), time.Now().Add(3*time.Second))
	stream := server.watchSandbox(t)
	live := untilSandbox(t, stream, func(*operatorpb.SandboxNotificationStatus) bool { return true })
	expiresAt := live.GetInboxes()[0].GetSubscriptions()[0].GetExpiresAt().AsTime()
	if !live.GetObservedAt().AsTime().Before(expiresAt) {
		t.Fatalf("the opening snapshot at %v should show the subscription live until %v", live.GetObservedAt().AsTime(), expiresAt)
	}
	// Nothing is committed from here on: the subscription expires with the clock.
	expired := untilSandbox(t, stream, func(*operatorpb.SandboxNotificationStatus) bool { return true })
	if observed := expired.GetObservedAt().AsTime(); observed.Before(expiresAt) {
		t.Errorf("a snapshot read at %v, before the subscription expired at %v, was sent as a change", observed, expiresAt)
	}
}

func TestSessionNoticeWatchOpensWithTheStatesAndFollowsCommittedChanges(t *testing.T) {
	server := newServer(t)
	subscription := server.subscribe(t, "notices", "noticed")
	stream := server.watchNotices(t, "noticed", "unknown")
	initial := untilNotices(t, stream, func(*operatorpb.SessionNoticeStates) bool { return true })
	if len(initial.GetInboxes()) != 1 || initial.GetInboxes()[0].GetQueued() != 0 {
		t.Fatalf("initial = %v", initial)
	}

	server.record(t, subscription, 2)
	queued := untilNotices(t, stream, func(s *operatorpb.SessionNoticeStates) bool { return s.GetInboxes()[0].GetQueued() == 2 })
	if inbox := queued.GetInboxes()[0]; inbox.GetNoticeWaitReason() != operatorpb.NoticeWaitReason_NOTICE_WAIT_REASON_DEBOUNCING || inbox.GetEligibleAt() == nil {
		t.Errorf("queued = %v", queued)
	}

	if _, err := server.client.AcknowledgeInbox(bearing(t, sandboxToken), &notificationspb.AcknowledgeInboxRequest{InboxId: subscription.GetInboxId(), ThroughCursor: 2}); err != nil {
		t.Fatal(err)
	}
	untilNotices(t, stream, func(s *operatorpb.SessionNoticeStates) bool {
		return s.GetInboxes()[0].GetQueued() == 0 && s.GetInboxes()[0].EligibleAt == nil
	})
}

func TestSessionNoticeWatchReadsAgainWhenADebounceFallsDue(t *testing.T) {
	server := newServer(t, func(c *serverConfig) { c.debounce = store.Debounce{Quiet: 2 * time.Second, MaxWait: 4 * time.Second} })
	subscription := server.subscribe(t, "notices-due", "noticed")
	server.record(t, subscription, 1)
	stream := server.watchNotices(t, "noticed")
	debouncing := untilNotices(t, stream, func(*operatorpb.SessionNoticeStates) bool { return true })
	if inbox := debouncing.GetInboxes()[0]; inbox.GetNoticeWaitReason() != operatorpb.NoticeWaitReason_NOTICE_WAIT_REASON_DEBOUNCING || inbox.GetEligibleAt() == nil {
		t.Fatalf("the opening snapshot should show the entry debouncing: %v", debouncing)
	}
	// Nothing is committed from here on: the notice falls due with the clock.
	due := untilNotices(t, stream, func(*operatorpb.SessionNoticeStates) bool { return true })
	if inbox := due.GetInboxes()[0]; inbox.GetNoticeWaitReason() != operatorpb.NoticeWaitReason_NOTICE_WAIT_REASON_READY || inbox.GetEligibleAt() != nil || inbox.GetQueued() != 1 {
		t.Errorf("a notice that is due shows %v", inbox)
	}
}

func TestWatchesEndWithReconnectRequiredWhenTheirLeaseEnds(t *testing.T) {
	server := newServer(t, func(c *serverConfig) { c.lease = time.Second })
	sandbox := server.watchSandbox(t)
	untilSandbox(t, sandbox, func(*operatorpb.SandboxNotificationStatus) bool { return true })
	wantSandboxReconnectAndCleanEnd(t, sandbox)

	notices := server.watchNotices(t, testSession)
	untilNotices(t, notices, func(*operatorpb.SessionNoticeStates) bool { return true })
	wantNoticesReconnectAndCleanEnd(t, notices)
}

func TestDrainingEndsOpenWatches(t *testing.T) {
	server := newServer(t)
	sandbox := server.watchSandbox(t)
	untilSandbox(t, sandbox, func(*operatorpb.SandboxNotificationStatus) bool { return true })
	notices := server.watchNotices(t, testSession)
	untilNotices(t, notices, func(*operatorpb.SessionNoticeStates) bool { return true })
	server.operatorService.Drain()
	wantSandboxReconnectAndCleanEnd(t, sandbox)
	wantNoticesReconnectAndCleanEnd(t, notices)
}
