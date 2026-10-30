package api

import (
	"context"
	"fmt"
	"strconv"
	"testing"
	"time"

	operatorpb "github.com/agentydragon/ducktape/agentplane/notification_service/notifications_operatorpb"
	"github.com/agentydragon/ducktape/agentplane/notification_service/notificationspb"
	"google.golang.org/grpc/codes"
)

// firstSandboxSnapshot is the state the stream opens with, as the account holding the token reads it.
func (s testServer) firstSandboxSnapshot(t *testing.T, token string, request *operatorpb.WatchSandboxNotificationsRequest) (*operatorpb.SandboxNotificationStatus, error) {
	t.Helper()
	ctx, cancel := context.WithCancel(bearing(t, token))
	defer cancel()
	stream, err := s.operator.WatchSandboxNotifications(ctx, request)
	if err != nil {
		t.Fatal(err)
	}
	response, err := stream.Recv()
	if err != nil {
		return nil, err
	}
	if response.GetSnapshot() == nil {
		t.Fatalf("the stream opened with %v, want a snapshot", response)
	}
	return response.GetSnapshot(), nil
}

func (s testServer) firstNoticeStates(t *testing.T, token string, sessionIDs ...string) (*operatorpb.SessionNoticeStates, error) {
	t.Helper()
	ctx, cancel := context.WithCancel(bearing(t, token))
	defer cancel()
	stream, err := s.operator.WatchSessionNotices(ctx, &operatorpb.WatchSessionNoticesRequest{SessionIds: sessionIDs})
	if err != nil {
		t.Fatal(err)
	}
	response, err := stream.Recv()
	if err != nil {
		return nil, err
	}
	if response.GetSnapshot() == nil {
		t.Fatalf("the stream opened with %v, want a snapshot", response)
	}
	return response.GetSnapshot(), nil
}

func sandboxWatchRequest(uid string) *operatorpb.WatchSandboxNotificationsRequest {
	return &operatorpb.WatchSandboxNotificationsRequest{Namespace: testNamespace, Name: sandboxName, Uid: uid}
}

func TestTheOperatorHasNoAgentInboxAuthority(t *testing.T) {
	server := newServer(t)
	subscription := server.subscribe(t, "operator-has-none")
	server.record(t, subscription, 1)

	inboxes, err := server.client.ListInboxes(bearing(t, operatorToken), &notificationspb.ListInboxesRequest{})
	if err != nil || len(inboxes.GetInboxes()) != 0 {
		t.Errorf("listed %v, %v", inboxes, err)
	}
	subscriptions, err := server.client.ListSubscriptions(bearing(t, operatorToken), &notificationspb.ListSubscriptionsRequest{})
	if err != nil || len(subscriptions.GetSubscriptions()) != 0 {
		t.Errorf("listed %v, %v", subscriptions, err)
	}
	_, err = server.client.ReadInbox(bearing(t, operatorToken), &notificationspb.ReadInboxRequest{InboxId: subscription.GetInboxId()})
	requireCode(t, err, codes.NotFound)
	_, err = server.client.AcknowledgeInbox(bearing(t, operatorToken), &notificationspb.AcknowledgeInboxRequest{InboxId: subscription.GetInboxId(), ThroughCursor: 1})
	requireCode(t, err, codes.NotFound)
}

func TestSandboxWatchRefusesASandboxItCannotRead(t *testing.T) {
	server := newServer(t)
	for name, c := range map[string]struct {
		change func(*operatorpb.WatchSandboxNotificationsRequest)
		want   codes.Code
	}{
		"another namespace": {func(r *operatorpb.WatchSandboxNotificationsRequest) { r.Namespace = "test-elsewhere" }, codes.NotFound},
		"no namespace":      {func(r *operatorpb.WatchSandboxNotificationsRequest) { r.Namespace = "" }, codes.NotFound},
		"no name":           {func(r *operatorpb.WatchSandboxNotificationsRequest) { r.Name = "" }, codes.InvalidArgument},
		"no UID":            {func(r *operatorpb.WatchSandboxNotificationsRequest) { r.Uid = "" }, codes.InvalidArgument},
	} {
		t.Run(name, func(t *testing.T) {
			request := sandboxWatchRequest(sandboxUID)
			c.change(request)
			_, err := server.firstSandboxSnapshot(t, operatorToken, request)
			requireCode(t, err, c.want)
		})
	}
}

func TestSandboxStatusIsReadOnlyAndPinnedToTheUID(t *testing.T) {
	server := newServer(t)
	subscription := server.subscribe(t, "status")
	server.record(t, subscription, 1)

	other, err := server.firstSandboxSnapshot(t, operatorToken, sandboxWatchRequest("another-uid"))
	if err != nil || len(other.GetInboxes()) != 0 {
		t.Fatalf("another incarnation's status = %v, %v", other, err)
	}
	shown, err := server.firstSandboxSnapshot(t, operatorToken, sandboxWatchRequest(sandboxUID))
	if err != nil || len(shown.GetInboxes()) != 1 {
		t.Fatalf("status = %v, %v", shown, err)
	}
	inbox := shown.GetInboxes()[0]
	if inbox.GetInbox().GetId() != subscription.GetInboxId() || inbox.GetUnannouncedCount() != 1 || inbox.GetPendingAcknowledgementCount() != 1 || inbox.GetPendingEntriesMore() {
		t.Errorf("inbox = %v", inbox)
	}
	if len(inbox.GetPendingEntries()) != 1 {
		t.Fatalf("pending entries = %v", inbox.GetPendingEntries())
	}
	entry := inbox.GetPendingEntries()[0]
	if want := fmt.Sprintf("Action %s · event 1 · decision_pending", subscription.GetSource().GetActions().GetRequestId()); entry.GetCursor() != 1 || entry.GetProvider() != "actions" || entry.GetSummary() != want {
		t.Errorf("pending entry = %v, want a summary %q", entry, want)
	}
	if len(inbox.GetSubscriptions()) != 1 || inbox.GetSubscriptions()[0].GetId() != subscription.GetId() {
		t.Errorf("subscriptions = %v", inbox.GetSubscriptions())
	}
	// With the server's 60s quiet window and 120s maximum wait, the single entry's notice is due when it quiets.
	if inbox.GetNoticeWaitReason() != operatorpb.NoticeWaitReason_NOTICE_WAIT_REASON_DEBOUNCING ||
		!inbox.GetNoticeDueAt().AsTime().Equal(inbox.GetQuietUntil().AsTime()) ||
		inbox.GetMaxWaitAt().AsTime().Sub(inbox.GetQuietUntil().AsTime()) != time.Minute {
		t.Errorf("notice wait = %v, due %v, quiet until %v, max wait %v", inbox.GetNoticeWaitReason(), inbox.GetNoticeDueAt(), inbox.GetQuietUntil(), inbox.GetMaxWaitAt())
	}

	// Reading acknowledged nothing, and the agent's acknowledgement shows.
	inboxes, err := server.client.ListInboxes(bearing(t, sandboxToken), &notificationspb.ListInboxesRequest{})
	if err != nil || len(inboxes.GetInboxes()) != 1 || inboxes.GetInboxes()[0].GetAcknowledged() != 0 {
		t.Fatalf("inboxes = %v, %v", inboxes, err)
	}
	if _, err := server.client.AcknowledgeInbox(bearing(t, sandboxToken), &notificationspb.AcknowledgeInboxRequest{InboxId: subscription.GetInboxId(), ThroughCursor: 1}); err != nil {
		t.Fatal(err)
	}
	acknowledged, err := server.firstSandboxSnapshot(t, operatorToken, sandboxWatchRequest(sandboxUID))
	if err != nil {
		t.Fatal(err)
	}
	if inbox := acknowledged.GetInboxes()[0]; len(inbox.GetPendingEntries()) != 0 || inbox.GetPendingAcknowledgementCount() != 0 {
		t.Errorf("acknowledged inbox = %v", inbox)
	}
}

func TestSandboxStatusSaysWhenMoreEntriesPendThanItLists(t *testing.T) {
	server := newServer(t)
	subscription := server.subscribe(t, "many")
	server.record(t, subscription, 101)
	shown, err := server.firstSandboxSnapshot(t, operatorToken, sandboxWatchRequest(sandboxUID))
	if err != nil || len(shown.GetInboxes()) != 1 {
		t.Fatalf("status = %v, %v", shown, err)
	}
	inbox := shown.GetInboxes()[0]
	if inbox.GetPendingAcknowledgementCount() != 101 || len(inbox.GetPendingEntries()) != 100 || !inbox.GetPendingEntriesMore() {
		t.Errorf("pending %d, listed %d, more %v", inbox.GetPendingAcknowledgementCount(), len(inbox.GetPendingEntries()), inbox.GetPendingEntriesMore())
	}
	if oldest := inbox.GetPendingEntries()[0].GetCursor(); oldest != 1 {
		t.Errorf("the oldest pending entry is %d, want 1", oldest)
	}
}

func TestSessionNoticeStatesCountQueuedEntriesWithoutAcknowledging(t *testing.T) {
	server := newServer(t)
	subscription := server.subscribe(t, "notice-state", "noticed")
	initial, err := server.firstNoticeStates(t, operatorToken, "noticed", "unknown")
	if err != nil {
		t.Fatal(err)
	}
	if len(initial.GetInboxes()) != 1 || initial.GetInboxes()[0].GetSessionId() != "noticed" || initial.GetInboxes()[0].GetQueued() != 0 ||
		initial.GetInboxes()[0].Notice != nil {
		t.Fatalf("initial = %v", initial)
	}

	server.record(t, subscription, 1)
	queued, err := server.firstNoticeStates(t, operatorToken, "noticed")
	if err != nil || len(queued.GetInboxes()) != 1 {
		t.Fatalf("queued = %v, %v", queued, err)
	}
	inbox := queued.GetInboxes()[0]
	if inbox.GetInboxId() != subscription.GetInboxId() || inbox.GetQueued() != 1 || inbox.GetUnacknowledged() != 0 ||
		inbox.GetNoticeWaitReason() != operatorpb.NoticeWaitReason_NOTICE_WAIT_REASON_DEBOUNCING || inbox.GetEligibleAt() == nil {
		t.Errorf("inbox = %v", inbox)
	}
	// Reading the state twice acknowledges nothing.
	if again, err := server.firstNoticeStates(t, operatorToken, "noticed"); err != nil || again.GetInboxes()[0].GetQueued() != 1 {
		t.Errorf("read again = %v, %v", again, err)
	}
	if _, err := server.client.AcknowledgeInbox(bearing(t, sandboxToken), &notificationspb.AcknowledgeInboxRequest{InboxId: subscription.GetInboxId(), ThroughCursor: 1}); err != nil {
		t.Fatal(err)
	}
	acknowledged, err := server.firstNoticeStates(t, operatorToken, "noticed")
	if err != nil || acknowledged.GetInboxes()[0].GetQueued() != 0 || acknowledged.GetInboxes()[0].EligibleAt != nil {
		t.Errorf("acknowledged = %v, %v", acknowledged, err)
	}
}

func TestSessionNoticeStatesRefuseMoreSessionsThanTheStoreReads(t *testing.T) {
	server := newServer(t)
	sessions := make([]string, 201)
	for i := range sessions {
		sessions[i] = "session-" + strconv.Itoa(i)
	}
	_, err := server.firstNoticeStates(t, operatorToken, sessions...)
	requireCode(t, err, codes.InvalidArgument)
}
