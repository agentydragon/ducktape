package api

import (
	"errors"
	"strings"
	"testing"
	"time"

	historypb "github.com/agentydragon/ducktape/agentplane/history_service/history_servicepb"
	"github.com/agentydragon/ducktape/agentplane/notification_service/notificationspb"
	"github.com/agentydragon/ducktape/agentplane/notification_service/sessionidentity"
	policyapi "github.com/agentydragon/ducktape/agentplane/policy/api/v1alpha1"
	"github.com/agentydragon/ducktape/agentplane/policy/reader"
	identitypb "github.com/agentydragon/ducktape/agentplane/proto/identity"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	dispatcherpb "github.com/agentydragon/ducktape/agentplane/runner_gateway/dispatcher/session_dispatcherpb"
	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	"github.com/google/uuid"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/encoding/protowire"
	"google.golang.org/protobuf/proto"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

// toSandbox targets the test Sandbox by name, whatever its incarnation.
var toSandbox = policyapi.SandboxTargets{Sandboxes: []policyapi.SandboxTarget{{Name: sandboxName}}}

// sendRequest is a message from the other Sandbox to the test Session of the test Sandbox as it is now.
func sendRequest(key string) *notificationspb.SendMessageRequest {
	return &notificationspb.SendMessageRequest{
		SessionId: testSession, IdempotencyKey: key, Body: "Please review the change.",
	}
}

func (s testServer) send(t *testing.T, request *notificationspb.SendMessageRequest) *notificationspb.Message {
	t.Helper()
	response, err := s.client.SendMessage(bearing(t, otherSandboxToken), request)
	if err != nil {
		t.Fatal(err)
	}
	return response.GetMessage()
}

func (s testServer) sentMessage(t *testing.T, token, id string) (*notificationspb.Message, error) {
	t.Helper()
	response, err := s.client.GetMessage(bearing(t, token), &notificationspb.GetMessageRequest{MessageId: id})
	return response.GetMessage(), err
}

// requireNoInbox checks that the test Sandbox has no inbox.
func (s testServer) requireNoInbox(t *testing.T) {
	t.Helper()
	inboxes, err := s.client.ListInboxes(bearing(t, sandboxToken), &notificationspb.ListInboxesRequest{})
	if err != nil {
		t.Fatal(err)
	}
	if len(inboxes.GetInboxes()) != 0 {
		t.Fatalf("inboxes = %v, want none", inboxes)
	}
}

func TestAGrantedSenderMessagesASessionThatReadsItFromItsInbox(t *testing.T) {
	server := newServer(t)
	server.grantSend(otherSandboxName, toSandbox)
	request := sendRequest("first")
	request.FromSession = proto.String("test-from-session")
	sent := server.send(t, request)
	if _, err := uuid.Parse(sent.GetId()); err != nil {
		t.Errorf("id %q: %v", sent.GetId(), err)
	}
	// The destination is pinned to the incarnation that took the message.
	if !proto.Equal(sent.GetDestinationRef(), sandboxRef()) || sent.GetSessionId() != testSession ||
		sent.GetIdempotencyKey() != "first" || time.Since(sent.GetAcceptedAt().AsTime()) > time.Minute ||
		sent.GetCovered() || sent.GetAcknowledged() || sent.GetInboxRetired() {
		t.Errorf("sent = %v", sent)
	}
	if retried := server.send(t, request); !proto.Equal(retried, sent) {
		t.Errorf("retry = %v, want %v", retried, sent)
	}

	inboxes, err := server.client.ListInboxes(bearing(t, sandboxToken), &notificationspb.ListInboxesRequest{})
	if err != nil {
		t.Fatal(err)
	}
	if len(inboxes.GetInboxes()) != 1 {
		t.Fatalf("inboxes = %v", inboxes)
	}
	read, err := server.client.ReadInbox(bearing(t, sandboxToken), &notificationspb.ReadInboxRequest{SessionId: testSession})
	if err != nil {
		t.Fatal(err)
	}
	if len(read.GetEntries()) != 1 {
		t.Fatalf("entries = %v", read.GetEntries())
	}
	entry := read.GetEntries()[0]
	want := &notificationspb.MessageEvent{
		MessageId: sent.GetId(), Sender: &identitypb.ServiceAccount{Namespace: testNamespace, Name: otherSandboxName},
	}
	if !proto.Equal(entry.GetEvent().GetMessage(), want) || len(entry.GetSubscriptionIds()) != 0 {
		t.Errorf("entry = %v", entry)
	}
	if payload := entry.GetPayload().AsMap(); payload["body"] != request.GetBody() || payload["from_session"] != "test-from-session" {
		t.Errorf("payload = %v", payload)
	}

	// Only the sender follows the message.
	if _, err := server.sentMessage(t, sandboxToken, sent.GetId()); status.Code(err) != codes.NotFound {
		t.Errorf("the recipient's lookup: %v, want NotFound", err)
	}
	if _, err := server.client.AcknowledgeInbox(bearing(t, sandboxToken), &notificationspb.AcknowledgeInboxRequest{SessionId: testSession, ThroughCursor: 1}); err != nil {
		t.Fatal(err)
	}
	followed, err := server.sentMessage(t, otherSandboxToken, sent.GetId())
	if err != nil {
		t.Fatal(err)
	}
	// No worker runs here, so no notice covers the message the recipient acknowledged.
	if !followed.GetAcknowledged() || followed.GetCovered() || !followed.GetAcceptedAt().AsTime().Equal(sent.GetAcceptedAt().AsTime()) {
		t.Errorf("after the acknowledgement: %v", followed)
	}

	// The History check establishes the new inbox. Later sends to this UID-keyed inbox work through
	// a History outage because they do not repeat it.
	server.history.failWith(status.Error(codes.Unavailable, "test History outage"))
	server.send(t, sendRequest("second"))
	if asked := server.history.questions(); len(asked) != 1 || asked[0] != testSession {
		t.Errorf("History asked about %v, want only the check that opened the inbox", asked)
	}
}

func TestASendNeedsAGrantCoveringTheSandboxAsItIsNow(t *testing.T) {
	cases := map[string]*policyapi.SandboxTargets{
		"no grant":            nil,
		"another Sandbox":     {Sandboxes: []policyapi.SandboxTarget{{Name: otherSandboxName}}},
		"another incarnation": {Sandboxes: []policyapi.SandboxTarget{{Name: sandboxName, UID: "test-former-sandbox-uid"}}},
	}
	for name, targets := range cases {
		t.Run(name, func(t *testing.T) {
			server := newServer(t)
			if targets != nil {
				server.grantSend(otherSandboxName, *targets)
			}
			// A grant to another account is not the sender's.
			server.grantSend(operatorName, toSandbox)
			_, err := server.client.SendMessage(bearing(t, otherSandboxToken), sendRequest("denied"))
			requireCode(t, err, codes.PermissionDenied)
			server.requireNoInbox(t)
		})
	}
}

func TestATagGrantCoversTheSandboxWhileItHoldsTheTag(t *testing.T) {
	server := newServer(t)
	server.grantSend(otherSandboxName, policyapi.SandboxTargets{InboxTags: []string{"test-group"}})
	_, err := server.client.SendMessage(bearing(t, otherSandboxToken), sendRequest("untagged"))
	requireCode(t, err, codes.PermissionDenied)
	tag := func(tags ...string) {
		t.Helper()
		var sandbox sandboxapi.Sandbox
		if err := server.kube.Get(t.Context(), client.ObjectKey{Namespace: testNamespace, Name: sandboxName}, &sandbox); err != nil {
			t.Fatal(err)
		}
		sandbox.Spec.InboxTags = tags
		if err := server.kube.Update(t.Context(), &sandbox); err != nil {
			t.Fatal(err)
		}
	}
	tag("test-other-group", "test-group")
	server.send(t, sendRequest("tagged"))
	tag("test-other-group")
	_, err = server.client.SendMessage(bearing(t, otherSandboxToken), sendRequest("untagged-again"))
	requireCode(t, err, codes.PermissionDenied)
}

func TestRevokingAGrantRefusesNewMessagesButKeepsTheSentOnes(t *testing.T) {
	server := newServer(t)
	server.grantSend(otherSandboxName, toSandbox)
	sent := server.send(t, sendRequest("before"))
	server.policy.Rules.DeleteBinding(otherSandboxName)
	_, err := server.client.SendMessage(bearing(t, otherSandboxToken), sendRequest("after"))
	requireCode(t, err, codes.PermissionDenied)
	if _, err := server.sentMessage(t, otherSandboxToken, sent.GetId()); err != nil {
		t.Fatalf("the sent message after the revocation: %v", err)
	}
}

func TestASendIsRefusedWhileTheGrantsMayBeOutOfDate(t *testing.T) {
	server := newServer(t)
	server.grantSend(otherSandboxName, toSandbox)
	server.policy.Health.RecordFailure(reader.PolicyBindings, errors.New("test: the watch failed"))
	_, err := server.client.SendMessage(bearing(t, otherSandboxToken), sendRequest("stale"))
	requireCode(t, err, codes.Unavailable)
	server.requireNoInbox(t)
}

func TestAMessageToNoSuchSessionOrSandboxIncarnationIsNotFound(t *testing.T) {
	server := newServer(t)
	server.grantSend(otherSandboxName, policyapi.SandboxTargets{AllSandboxes: true})
	for name, placement := range map[string]*dispatcherpb.Session{
		"missing Sandbox":     nativeSession(testSession, "test-missing-sandbox", sandboxUID),
		"another incarnation": nativeSession(testSession, sandboxName, "test-former-sandbox-uid"),
	} {
		t.Run(name, func(t *testing.T) {
			server.openings.placements[testSession] = placement
			request := sendRequest("missing")
			_, err := server.client.SendMessage(bearing(t, otherSandboxToken), request)
			requireCode(t, err, codes.NotFound)
		})
	}
	delete(server.openings.placements, testSession)
	_, err := server.client.SendMessage(bearing(t, otherSandboxToken), sendRequest("missing-opening"))
	requireCode(t, err, codes.NotFound)
	server.requireNoInbox(t)
}

func TestAMessageNeedsACompleteDispatcherSession(t *testing.T) {
	server := newServer(t)
	server.grantSend(otherSandboxName, toSandbox)
	for name, placement := range map[string]*dispatcherpb.Session{
		"missing UID":     nativeSession(testSession, sandboxName, ""),
		"wrong Session":   nativeSession("another-session", sandboxName, sandboxUID),
		"missing Sandbox": nativeSession(testSession, "", sandboxUID),
	} {
		t.Run(name, func(t *testing.T) {
			server.openings.placements[testSession] = placement
			_, err := server.client.SendMessage(bearing(t, otherSandboxToken), sendRequest("incomplete"))
			requireCode(t, err, codes.Unavailable)
		})
	}
	server.openings.err = status.Error(codes.Unavailable, "test Dispatcher outage")
	_, err := server.client.SendMessage(bearing(t, otherSandboxToken), sendRequest("outage"))
	requireCode(t, err, codes.Unavailable)
	server.requireNoInbox(t)
	if asked := server.history.questions(); len(asked) != 0 {
		t.Errorf("History asked about %v without a valid opening", asked)
	}
}

func TestSessionTargetUsesTypedPlacement(t *testing.T) {
	// Placement is read without a server, so the UID is any exact one.
	const sandboxUID = "test-sandbox-uid"
	central := &dispatcherpb.Session{
		SessionId: testSession,
		Placement: &sessionpb.SessionPlacement{Kind: &sessionpb.SessionPlacement_CentralSamplerV1{CentralSamplerV1: &sessionpb.CentralSamplerV1Placement{
			ExecutorEnvironment: &sessionpb.ExactEnvironment{Name: sandboxName, Uid: sandboxUID},
		}}},
	}
	for _, test := range []struct {
		name    string
		opening *dispatcherpb.Session
		want    bool
	}{
		{"native environment", nativeSession(testSession, sandboxName, sandboxUID), true},
		{"central initial executor", central, true},
		{"missing placement", &dispatcherpb.Session{SessionId: testSession}, false},
		{"missing central executor", &dispatcherpb.Session{SessionId: testSession, Placement: &sessionpb.SessionPlacement{Kind: &sessionpb.SessionPlacement_CentralSamplerV1{CentralSamplerV1: &sessionpb.CentralSamplerV1Placement{}}}}, false},
		{"wrong Session", nativeSession("other", sandboxName, sandboxUID), false},
	} {
		t.Run(test.name, func(t *testing.T) {
			target := sessionidentity.Target(test.opening, testSession)
			if (target != nil) != test.want {
				t.Fatalf("sessionidentity.Target(%v) = %v, want present %t", test.opening, target, test.want)
			}
			if test.want && (target.GetName() != sandboxName || target.GetUid() != sandboxUID) {
				t.Fatalf("wrong exact target: %v", target)
			}
		})
	}
}

func TestAnOldWireDestinationFenceIsRejectedBeforeSending(t *testing.T) {
	server := newServer(t)
	server.grantSend(otherSandboxName, toSandbox)
	oldRef, err := proto.Marshal(&notificationspb.DestinationRef{
		Namespace: testNamespace, Name: sandboxName, Uid: "test-former-sandbox-uid",
	})
	if err != nil {
		t.Fatal(err)
	}
	request := sendRequest("old-wire")
	request.ProtoReflect().SetUnknown(protowire.AppendBytes(protowire.AppendTag(nil, 1, protowire.BytesType), oldRef))
	_, err = server.client.SendMessage(bearing(t, otherSandboxToken), request)
	requireCode(t, err, codes.InvalidArgument)
	server.requireNoInbox(t)
	if asked := server.history.questions(); len(asked) != 0 {
		t.Errorf("History asked about %v after an old wire request", asked)
	}
}

func TestAMessageOpensAnInboxOnlyForSessionHistoryOfTheExactSandbox(t *testing.T) {
	server := newServer(t)
	server.grantSend(otherSandboxName, toSandbox)
	server.history.sandboxes["test-other-sandbox-session"] = &historypb.SandboxRef{
		Namespace: testNamespace, Name: otherSandboxName, Uid: proto.String(otherSandboxUID),
	}
	server.history.sandboxes["test-former-sandbox-session"] = &historypb.SandboxRef{
		Namespace: testNamespace, Name: sandboxName, Uid: proto.String("test-former-sandbox-uid"),
	}
	server.history.sandboxes["test-session-without-history-uid"] = &historypb.SandboxRef{
		Namespace: testNamespace, Name: sandboxName,
	}
	for name, session := range map[string]string{
		"unrecorded Session":          "test-unrecorded-session",
		"another Sandbox's Session":   "test-other-sandbox-session",
		"same-name predecessor":       "test-former-sandbox-session",
		"History Session without UID": "test-session-without-history-uid",
	} {
		t.Run(name, func(t *testing.T) {
			server.openings.placements[session] = nativeSession(session, sandboxName, sandboxUID)
			request := sendRequest("unrecorded")
			request.SessionId = session
			_, err := server.client.SendMessage(bearing(t, otherSandboxToken), request)
			requireCode(t, err, codes.NotFound)
		})
	}
	server.history.failWith(status.Error(codes.Unavailable, "test History outage"))
	_, err := server.client.SendMessage(bearing(t, otherSandboxToken), sendRequest("outage"))
	requireCode(t, err, codes.Unavailable)
	server.requireNoInbox(t)
}

func TestMessageToSameNameReplacementDoesNotReuseFormerSandboxInbox(t *testing.T) {
	server := newServer(t)
	server.grantSend(otherSandboxName, toSandbox)
	first := server.send(t, sendRequest("before-recreate"))

	server.recreateSandbox(t)
	_, err := server.client.SendMessage(bearing(t, otherSandboxToken), sendRequest("after-recreate"))
	requireCode(t, err, codes.NotFound)
	server.requireNoInbox(t)
	if _, err := server.sentMessage(t, otherSandboxToken, first.GetId()); err != nil {
		t.Fatalf("the earlier message after Sandbox replacement: %v", err)
	}
}

func TestConfirmSessionRequiresLiveSandboxUID(t *testing.T) {
	server := newServer(t)
	err := server.notifications.confirmSession(t.Context(), &identitypb.EnvironmentTarget{
		Namespace: testNamespace, Name: sandboxName,
	}, testSession)
	if !errors.Is(err, errSessionUnrecorded) {
		t.Fatalf("confirmSession error = %v, want %v", err, errSessionUnrecorded)
	}
}

func TestAMessageMustFitTheConfiguredLimit(t *testing.T) {
	server := newServer(t)
	server.grantSend(otherSandboxName, toSandbox)
	request := sendRequest("at-limit")
	request.Body = strings.Repeat("b", server.config.messageBodyLimit)
	server.send(t, request)
	request = sendRequest("past-limit")
	request.Body = strings.Repeat("b", server.config.messageBodyLimit+1)
	_, err := server.client.SendMessage(bearing(t, otherSandboxToken), request)
	requireCode(t, err, codes.InvalidArgument)
}
