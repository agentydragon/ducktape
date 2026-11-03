package api

import (
	"io"
	"testing"
	"time"

	pb "github.com/agentydragon/ducktape/agentplane/notification_service/notificationspb"
	"github.com/agentydragon/ducktape/agentplane/notification_service/store"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
)

type inboxWatch = grpc.ServerStreamingClient[pb.WatchInboxResponse]

func (s testServer) watchInbox(t *testing.T, session string) inboxWatch {
	t.Helper()
	stream, err := s.client.WatchInbox(bearing(t, operatorToken), &pb.WatchInboxRequest{SessionId: session})
	if err != nil {
		t.Fatal(err)
	}
	return stream
}

func untilInbox(t *testing.T, stream inboxWatch, want func(*pb.Inbox) bool) *pb.Inbox {
	t.Helper()
	for {
		response, err := stream.Recv()
		if err != nil {
			t.Fatalf("watch ended before expected state: %v", err)
		}
		if inbox := response.GetInbox(); inbox != nil && want(inbox) {
			return inbox
		}
	}
}

func TestInboxWatchOpensAndFollowsCommittedChanges(t *testing.T) {
	s := newServer(t)
	stream := s.watchInbox(t, testSession)
	first, err := stream.Recv()
	if err != nil || first.GetNoInbox() == nil {
		t.Fatalf("initial state = %v, %v", first, err)
	}
	sub := s.subscribe(t, "stream")
	untilInbox(t, stream, func(i *pb.Inbox) bool { return i.GetId() == sub.GetInboxId() })
	s.record(t, sub, 1)
	untilInbox(t, stream, func(i *pb.Inbox) bool { return i.GetDelivery().GetPendingAcknowledgementCount() == 1 })
	if _, err := s.client.AcknowledgeInbox(bearing(t, sandboxToken), &pb.AcknowledgeInboxRequest{InboxId: sub.GetInboxId(), ThroughCursor: 1}); err != nil {
		t.Fatal(err)
	}
	untilInbox(t, stream, func(i *pb.Inbox) bool { return i.GetAcknowledged() == 1 })
	if _, err := s.client.RetireInbox(bearing(t, sandboxToken), &pb.RetireInboxRequest{InboxId: sub.GetInboxId()}); err != nil {
		t.Fatal(err)
	}
	retired := untilInbox(t, stream, func(i *pb.Inbox) bool { return i.GetRetired() })
	if retired.GetDelivery().GetWaitReason() != pb.NoticeWaitReason_NOTICE_WAIT_REASON_RETIRED {
		t.Fatalf("retired: %v", retired)
	}
}

func TestInboxWatchUpdatesWhenDebounceFallsDue(t *testing.T) {
	s := newServer(t, func(c *serverConfig) { c.debounce = store.Debounce{Quiet: 2 * time.Second, MaxWait: 4 * time.Second} })
	sub := s.subscribe(t, "due")
	s.record(t, sub, 1)
	stream := s.watchInbox(t, testSession)
	untilInbox(t, stream, func(i *pb.Inbox) bool {
		return i.GetDelivery().GetWaitReason() == pb.NoticeWaitReason_NOTICE_WAIT_REASON_DEBOUNCING
	})
	untilInbox(t, stream, func(i *pb.Inbox) bool {
		return i.GetDelivery().GetWaitReason() == pb.NoticeWaitReason_NOTICE_WAIT_REASON_READY
	})
}

func TestSubscriptionWatchUpdatesWhenExpiryFallsDue(t *testing.T) {
	s := newServer(t)
	sub := s.subscribe(t, "expires")
	s.exec(t, `UPDATE subscription SET expires_at = $2 WHERE id = $1`, sub.GetId(), time.Now().Add(2*time.Second))
	stream, err := s.client.WatchSubscriptions(bearing(t, operatorToken), &pb.WatchSubscriptionsRequest{SessionId: testSession})
	if err != nil {
		t.Fatal(err)
	}
	first, err := stream.Recv()
	if err != nil || len(first.GetSnapshot().GetSubscriptions()) != 1 || first.GetSnapshot().GetSubscriptions()[0].GetExpired() {
		t.Fatalf("initial = %v, %v", first, err)
	}
	for {
		frame, err := stream.Recv()
		if err != nil {
			t.Fatal(err)
		}
		if delta := frame.GetDelta(); delta != nil && len(delta.GetUpserted()) == 1 && delta.GetUpserted()[0].GetExpired() {
			break
		}
	}
}

func TestDrainingEndsOpenWatches(t *testing.T) {
	s := newServer(t)
	stream := s.watchInbox(t, testSession)
	if _, err := stream.Recv(); err != nil {
		t.Fatal(err)
	}
	s.notifications.Drain()
	if _, err := stream.Recv(); err != io.EOF {
		t.Fatalf("drain end = %v", err)
	}
}

func TestEntryWatchSendsChangesAndRemovesAcknowledgedEntries(t *testing.T) {
	s := newServer(t)
	sub := s.subscribe(t, "entry-stream")
	stream, err := s.client.WatchInboxEntries(bearing(t, operatorToken), &pb.WatchInboxEntriesRequest{SessionId: testSession})
	if err != nil {
		t.Fatal(err)
	}
	initial, err := stream.Recv()
	if err != nil || initial.GetSnapshot() == nil || len(initial.GetSnapshot().GetEntries()) != 0 {
		t.Fatalf("initial = %v, %v", initial, err)
	}
	s.record(t, sub, 1)
	for {
		frame, err := stream.Recv()
		if err != nil {
			t.Fatal(err)
		}
		if delta := frame.GetDelta(); delta != nil && len(delta.GetUpserted()) == 1 && delta.GetUpserted()[0].GetCursor() == 1 {
			break
		}
	}
	if _, err := s.client.AcknowledgeInbox(bearing(t, sandboxToken), &pb.AcknowledgeInboxRequest{InboxId: sub.GetInboxId(), ThroughCursor: 1}); err != nil {
		t.Fatal(err)
	}
	for {
		frame, err := stream.Recv()
		if err != nil {
			t.Fatal(err)
		}
		if delta := frame.GetDelta(); delta != nil && len(delta.GetRemovedCursors()) == 1 && delta.GetRemovedCursors()[0] == 1 {
			break
		}
	}
}

func TestWorkloadWatchStopsWhenItsSandboxIsRecreated(t *testing.T) {
	s := newServer(t)
	sub := s.subscribe(t, "watch-fence")
	stream, err := s.client.WatchInbox(bearing(t, sandboxToken), &pb.WatchInboxRequest{SessionId: testSession})
	if err != nil {
		t.Fatal(err)
	}
	if first, err := stream.Recv(); err != nil || first.GetInbox() == nil {
		t.Fatalf("initial = %v, %v", first, err)
	}
	s.recreateSandbox(t)
	s.record(t, sub, 1)
	_, err = stream.Recv()
	requireCode(t, err, codes.NotFound)
}
