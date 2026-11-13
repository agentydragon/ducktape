package rpc

import (
	"context"
	"strings"
	"testing"

	pb "github.com/agentydragon/ducktape/agentplane/notification_service/api/notifications/notificationspb"
	"google.golang.org/grpc/codes"
)

func TestOperatorInspectionDoesNotGrantInboxMutation(t *testing.T) {
	server := newServer(t)
	subscription := server.subscribe(t, "operator-has-none")
	server.record(t, subscription, 1)
	inboxes, err := server.client.ListInboxes(bearing(t, operatorToken), &pb.ListInboxesRequest{})
	requireCode(t, err, codes.NotFound)
	if err == nil && len(inboxes.GetInboxes()) != 0 {
		t.Errorf("listed %v, %v", inboxes, err)
	}
	subscriptions, err := server.client.ListSubscriptions(bearing(t, operatorToken), &pb.ListSubscriptionsRequest{})
	requireCode(t, err, codes.NotFound)
	if err == nil && len(subscriptions.GetSubscriptions()) != 0 {
		t.Errorf("listed %v, %v", subscriptions, err)
	}
	page, err := server.client.ReadInbox(bearing(t, operatorToken), &pb.ReadInboxRequest{SessionId: testSession})
	if err != nil || len(page.GetEntries()) != 1 || page.GetEntries()[0].GetCursor() != 1 || page.GetInbox().GetAcknowledged() != 0 {
		t.Fatalf("operator read = %v, %v", page, err)
	}
	_, err = server.client.AcknowledgeInbox(bearing(t, operatorToken), &pb.AcknowledgeInboxRequest{SessionId: testSession, ThroughCursor: 1})
	requireCode(t, err, codes.NotFound)
	_, err = server.client.RetireInbox(bearing(t, operatorToken), &pb.RetireInboxRequest{SessionId: testSession})
	requireCode(t, err, codes.NotFound)
}

func TestOperatorCanInspectRetainedInboxAfterSandboxReplacement(t *testing.T) {
	s := newServer(t)
	sub := s.subscribe(t, "retained-after-delete")
	s.record(t, sub, 1)
	s.recreateSandbox(t)

	page, err := s.client.ReadInbox(bearing(t, operatorToken), &pb.ReadInboxRequest{SessionId: testSession})
	if err != nil || page.GetInbox().GetId() != sub.GetInboxId() || len(page.GetEntries()) != 1 || page.GetEntries()[0].GetPayload() == nil {
		t.Fatalf("retained operator read = %v, %v", page, err)
	}
	_, err = s.client.ReadInbox(bearing(t, sandboxToken), &pb.ReadInboxRequest{SessionId: testSession})
	requireCode(t, err, codes.NotFound)
	_, err = s.client.ReadInbox(bearing(t, otherSandboxToken), &pb.ReadInboxRequest{SessionId: testSession})
	requireCode(t, err, codes.NotFound)
	_, err = s.client.AcknowledgeInbox(bearing(t, operatorToken), &pb.AcknowledgeInboxRequest{SessionId: testSession, ThroughCursor: 1})
	requireCode(t, err, codes.NotFound)
	_, err = s.client.RetireInbox(bearing(t, operatorToken), &pb.RetireInboxRequest{SessionId: testSession})
	requireCode(t, err, codes.NotFound)
}

func TestOperatorPayloadReadRequiresConfiguredAccount(t *testing.T) {
	s := newServer(t, func(config *serverConfig) { config.operatorReader = "" })
	sub := s.subscribe(t, "no-operator-reader")
	s.record(t, sub, 1)
	_, err := s.client.ReadInbox(bearing(t, operatorToken), &pb.ReadInboxRequest{SessionId: testSession})
	requireCode(t, err, codes.NotFound)
}

func TestSessionInboxReadsAreAuthorizedAndDoNotAcknowledge(t *testing.T) {
	s := newServer(t)
	sub := s.subscribe(t, "session-status")
	s.record(t, sub, 1)
	for _, token := range []string{operatorToken, identityToken} {
		got, err := s.client.GetInbox(bearing(t, token), &pb.GetInboxRequest{SessionId: testSession})
		if err != nil || got.GetInbox().GetId() != sub.GetInboxId() || got.GetInbox().GetAcknowledged() != 0 || got.GetInbox().GetDelivery().GetPendingAcknowledgementCount() != 1 {
			t.Fatalf("GetInbox for %s = %v, %v", token, got, err)
		}
	}
	_, err := s.client.GetInbox(bearing(t, otherSandboxToken), &pb.GetInboxRequest{SessionId: testSession})
	requireCode(t, err, codes.NotFound)
	_, err = s.client.GetInbox(bearing(t, operatorToken), &pb.GetInboxRequest{SessionId: ""})
	requireCode(t, err, codes.InvalidArgument)
	_, err = s.client.GetInbox(bearing(t, operatorToken), &pb.GetInboxRequest{SessionId: strings.Repeat("x", 201)})
	requireCode(t, err, codes.InvalidArgument)
	missing, err := s.client.GetInbox(bearing(t, operatorToken), &pb.GetInboxRequest{SessionId: "unknown"})
	if err != nil || missing.GetInbox() != nil {
		t.Fatalf("missing inbox = %v, %v", missing, err)
	}

	ctx, cancel := context.WithCancel(bearing(t, operatorToken))
	defer cancel()
	entries, err := s.client.WatchInboxEntries(ctx, &pb.WatchInboxEntriesRequest{SessionId: testSession})
	if err != nil {
		t.Fatal(err)
	}
	frame, err := entries.Recv()
	if err != nil || len(frame.GetSnapshot().GetEntries()) != 1 {
		t.Fatalf("entries = %v, %v", frame, err)
	}
	entry := frame.GetSnapshot().GetEntries()[0]
	if entry.GetCursor() != 1 || entry.GetEvent().GetActions().GetRequestId() != sub.GetSource().GetActions().GetRequestId() {
		t.Fatalf("summary = %v", entry)
	}
	watches, err := s.client.WatchSubscriptions(ctx, &pb.WatchSubscriptionsRequest{SessionId: testSession})
	if err != nil {
		t.Fatal(err)
	}
	subscriptions, err := watches.Recv()
	if err != nil || len(subscriptions.GetSnapshot().GetSubscriptions()) != 1 {
		t.Fatalf("subscriptions = %v, %v", subscriptions, err)
	}
	shown := subscriptions.GetSnapshot().GetSubscriptions()[0]
	if shown.GetId() != sub.GetId() || shown.IdempotencyKey != nil || shown.Version != nil {
		t.Fatalf("operator subscription = %v", shown)
	}
}

func TestEntryWatchBoundsItsSnapshot(t *testing.T) {
	s := newServer(t)
	sub := s.subscribe(t, "many")
	s.record(t, sub, 101)
	stream, err := s.client.WatchInboxEntries(bearing(t, operatorToken), &pb.WatchInboxEntriesRequest{SessionId: testSession})
	if err != nil {
		t.Fatal(err)
	}
	response, err := stream.Recv()
	if err != nil || len(response.GetSnapshot().GetEntries()) != 100 || !response.GetSnapshot().GetMore() || response.GetSnapshot().GetEntries()[0].GetCursor() != 1 {
		t.Fatalf("bounded snapshot = %v, %v", response, err)
	}
}
