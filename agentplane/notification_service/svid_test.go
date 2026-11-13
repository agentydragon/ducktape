package main

import (
	"context"
	"crypto/x509"
	"testing"
	"time"

	actionpb "github.com/agentydragon/ducktape/agentplane/action_service/action_servicepb"
	"github.com/agentydragon/ducktape/agentplane/environment_runtime_transport"
	"github.com/agentydragon/ducktape/agentplane/environment_runtime_transport/testing"
	historypb "github.com/agentydragon/ducktape/agentplane/history_service/api/history/history_servicepb"
	"github.com/agentydragon/ducktape/agentplane/identity/auth/listener"
	"github.com/agentydragon/ducktape/agentplane/identity/auth/testing"
	"github.com/agentydragon/ducktape/agentplane/notification_service/internal/store"
	"github.com/agentydragon/ducktape/agentplane/notification_service/model"
	"github.com/agentydragon/ducktape/agentplane/notification_service/notificationspb"
	notificationtesting "github.com/agentydragon/ducktape/agentplane/notification_service/testing" // Shared gRPC fixtures; avoids colliding with Go testing.
	policyapi "github.com/agentydragon/ducktape/agentplane/policy/api/v1alpha1"
	identitypb "github.com/agentydragon/ducktape/agentplane/proto/identity"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	dispatcherpb "github.com/agentydragon/ducktape/agentplane/runner_gateway/dispatcher/session_dispatcherpb"
	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	"github.com/google/uuid"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

func TestSVIDListenerRetainsLegacyInboxAndRejectsDeletedSandbox(t *testing.T) {
	workload := workloadauthtesting.NewIdentityWorkload(t, kube, testNamespace, "svid-"+uuid.NewString(), testNamespace)
	other := workloadauthtesting.NewIdentityWorkload(t, kube, testNamespace, "svid-"+uuid.NewString(), testNamespace)
	setSandboxIdentity(t, &workload)
	setSandboxIdentity(t, &other)
	ca := environmentruntimetransporttesting.NewCA(t)
	serving := ca.IssueWithUsage(t, environment_runtime_transport.NotificationsURI, x509.ExtKeyUsageServerAuth)
	service := start(t, func(c *config) {
		c.RPC.TLS = &listener.TLS{CertFile: serving.CertFile, KeyFile: serving.KeyFile}
		c.RPC.Auth = append(c.RPC.Auth, &listener.SPIFFEAuth{CAFile: serving.CAFile})
	})
	inboxes := store.New(service.db, defaultConfig().Quotas)
	ref := model.DestinationRef{Namespace: testNamespace, Name: workload.Sandbox.Name, UID: string(workload.Sandbox.UID)}
	if err := inboxes.OpenInbox(t.Context(), store.Owner{Identity: model.Identity{Name: workload.Identity.Name, UID: string(workload.Identity.UID)}, SandboxUID: ref.UID}, ref, "svid-session"); err != nil {
		t.Fatal(err)
	}
	if _, err := service.db.ExecContext(t.Context(), `UPDATE inbox SET owner_uid = $2,
		owner_identity_name = NULL, owner_identity_uid = NULL WHERE session_id = $1`, "svid-session", workload.Principal.ServiceAccountUID); err != nil {
		t.Fatal(err)
	}
	for i, caller := range []workloadauthtesting.IdentityWorkload{workload, other} {
		config := ca.Issue(t, caller.URI)
		credentials, err := config.ClientCredentials(environment_runtime_transport.NotificationsURI)
		if err != nil {
			t.Fatal(err)
		}
		conn, err := grpc.NewClient(service.rpcAddress, grpc.WithTransportCredentials(credentials))
		if err != nil {
			t.Fatal(err)
		}
		t.Cleanup(func() { conn.Close() })
		ctx, cancel := context.WithTimeout(t.Context(), callDeadline)
		defer cancel()
		response, err := notificationspb.NewNotificationsClient(conn).ListInboxes(ctx, &notificationspb.ListInboxesRequest{})
		if err != nil {
			t.Fatal(err)
		}
		_, mixedErr := notificationspb.NewNotificationsClient(conn).ListInboxes(
			metadata.AppendToOutgoingContext(ctx, "authorization", "Bearer "+sandboxToken), &notificationspb.ListInboxesRequest{},
		)
		if status.Code(mixedErr) != codes.Unauthenticated {
			t.Fatalf("SVID plus bearer authenticated: %v", mixedErr)
		}
		want := 0
		if len(response.GetInboxes()) != want {
			t.Fatalf("caller %s sees %d inboxes, want %d", caller.Sandbox.Name, len(response.GetInboxes()), want)
		}
		if i == 0 {
			caller.Workload.Delete(t)
			denied, cancel := context.WithTimeout(t.Context(), 5*time.Second)
			_, err = notificationspb.NewNotificationsClient(conn).ListInboxes(denied, &notificationspb.ListInboxesRequest{})
			cancel()
			if status.Code(err) != codes.Unauthenticated {
				t.Fatalf("deleted Sandbox authenticated on existing TLS connection: %v", err)
			}
		}
	}
}

// An Identity caller owns the inboxes of its Sandbox's Sessions the Session authority recorded as
// executing as its Identity incarnation.
func TestSVIDIdentityCallerOwnsItsSessionsInboxes(t *testing.T) {
	workload := workloadauthtesting.NewIdentityWorkload(t, kube, testNamespace, "svid-identity-"+uuid.NewString(), testNamespace)
	other := workloadauthtesting.NewIdentityWorkload(t, kube, testNamespace, "svid-identity-"+uuid.NewString(), testNamespace)
	setSandboxIdentity(t, &workload)
	setSandboxIdentity(t, &other)
	ca := environmentruntimetransporttesting.NewCA(t)
	serving := ca.IssueWithUsage(t, environment_runtime_transport.NotificationsURI, x509.ExtKeyUsageServerAuth)
	service := start(t, func(c *config) {
		c.RPC.TLS = &listener.TLS{CertFile: serving.CertFile, KeyFile: serving.KeyFile}
		c.RPC.Auth = append(c.RPC.Auth, &listener.SPIFFEAuth{CAFile: serving.CAFile})
	})
	inboxes := store.New(service.db, defaultConfig().Quotas)
	ref := model.DestinationRef{Namespace: testNamespace, Name: workload.Sandbox.Name, UID: string(workload.Sandbox.UID)}
	if err := inboxes.OpenInbox(t.Context(), store.Owner{Identity: model.Identity{Name: workload.Identity.Name, UID: string(workload.Identity.UID)}, SandboxUID: ref.UID}, ref, "svid-identity-session"); err != nil {
		t.Fatal(err)
	}
	connect := func(caller workloadauthtesting.Workload) notificationspb.NotificationsClient {
		credentials, err := ca.Issue(t, caller.URI).ClientCredentials(environment_runtime_transport.NotificationsURI)
		if err != nil {
			t.Fatal(err)
		}
		conn, err := grpc.NewClient(service.rpcAddress, grpc.WithTransportCredentials(credentials))
		if err != nil {
			t.Fatal(err)
		}
		t.Cleanup(func() { conn.Close() })
		return notificationspb.NewNotificationsClient(conn)
	}
	ctx, cancel := context.WithTimeout(t.Context(), callDeadline)
	defer cancel()
	for caller, want := range map[*workloadauthtesting.IdentityWorkload]int{&workload: 1, &other: 0} {
		response, err := connect(caller.Workload).ListInboxes(ctx, &notificationspb.ListInboxesRequest{})
		if err != nil {
			t.Fatal(err)
		}
		if got := len(response.GetInboxes()); got != want {
			t.Fatalf("Identity %s sees %d inboxes, want %d", caller.Identity.Name, got, want)
		}
	}
}

// identityRunDispatcher records testSession as executing as an Identity alone in a Sandbox.
type identityRunDispatcher struct {
	dispatcherpb.UnimplementedSessionDispatcherServer
	sandbox  *sandboxapi.Sandbox
	identity *policyapi.Identity
}

func (d identityRunDispatcher) GetSession(_ context.Context, request *dispatcherpb.GetSessionRequest) (*dispatcherpb.GetSessionResponse, error) {
	if request.GetSessionId() != testSession {
		return nil, status.Error(codes.NotFound, "opening not found")
	}
	return &dispatcherpb.GetSessionResponse{Session: &dispatcherpb.Session{
		SessionId: testSession,
		Identity:  &identitypb.Identity{Name: d.identity.Name, Uid: string(d.identity.UID)},
		Placement: &sessionpb.SessionPlacement{Kind: &sessionpb.SessionPlacement_EnvironmentHosted{EnvironmentHosted: &sessionpb.EnvironmentHostedPlacement{
			Environment: &sessionpb.ExactEnvironment{Name: d.sandbox.Name, Uid: string(d.sandbox.UID)},
		}}},
	}}, nil
}

// sandboxHistory records every Session as written by one Sandbox.
type sandboxHistory struct {
	historypb.UnimplementedHistoryServiceServer
	sandbox *sandboxapi.Sandbox
}

func (h sandboxHistory) GetSessionSummary(_ context.Context, request *historypb.GetSessionSummaryRequest) (*historypb.SessionSummary, error) {
	return &historypb.SessionSummary{SessionId: request.GetSessionId(), Sandbox: &historypb.SandboxRef{
		Namespace: h.sandbox.Namespace, Name: h.sandbox.Name, Uid: proto.String(string(h.sandbox.UID)),
	}}, nil
}

// identityActions says every Action request was submitted by one Identity incarnation.
type identityActions struct {
	actionpb.UnimplementedActionServiceServer
	identity *policyapi.Identity
}

func (a identityActions) GetActionRequest(context.Context, *actionpb.GetActionRequestRequest) (*actionpb.GetActionRequestResponse, error) {
	return &actionpb.GetActionRequestResponse{ActionRequest: &actionpb.ActionRequest{
		CallerIdentity: &identitypb.Identity{Name: a.identity.Name, Uid: string(a.identity.UID)},
	}}, nil
}

func setSandboxIdentity(t *testing.T, workload *workloadauthtesting.IdentityWorkload) {
	t.Helper()
	current := workload.Sandbox.DeepCopy()
	current.Spec.Identity = &sandboxapi.IdentityRef{Name: workload.Identity.Name}
	if err := workload.Objects.Update(t.Context(), current); err != nil {
		t.Fatal(err)
	}
	workload.Sandbox = current
}

func (identityActions) ListActionRequestEvents(context.Context, *actionpb.ListActionRequestEventsRequest) (*actionpb.ListActionRequestEventsResponse, error) {
	return &actionpb.ListActionRequestEventsResponse{}, nil
}

// In-Sandbox agents message a Session as Identities: a Session of the sender's own Identity takes the message
// with no binding, another Identity's needs a binding naming the sender's Identity, the message lands in the
// inbox the recipient Session's Identity owns naming the sending Identity, only the sender follows it, and the
// recipient subscribes its Session to an Action request its Identity submitted.
func TestAnIdentityMessagesAndSubscribesForAnIdentityRunSession(t *testing.T) {
	recipient := workloadauthtesting.NewIdentityWorkload(t, kube, testNamespace, "svid-recipient-"+uuid.NewString()[:8], testNamespace)
	sender := workloadauthtesting.NewIdentityWorkload(t, kube, testNamespace, "svid-sender-"+uuid.NewString()[:8], testNamespace)
	stranger := workloadauthtesting.NewIdentityWorkload(t, kube, testNamespace, "svid-stranger-"+uuid.NewString()[:8], testNamespace)
	setSandboxIdentity(t, &recipient)
	setSandboxIdentity(t, &sender)
	setSandboxIdentity(t, &stranger)
	dispatcherAddress := notificationtesting.ServeGRPC(t, func(s *grpc.Server) {
		dispatcherpb.RegisterSessionDispatcherServer(s, identityRunDispatcher{sandbox: recipient.Sandbox, identity: recipient.Identity})
	})
	historyAddress := notificationtesting.ServeGRPC(t, func(s *grpc.Server) {
		historypb.RegisterHistoryServiceServer(s, sandboxHistory{sandbox: recipient.Sandbox})
	})
	actionAddress := notificationtesting.ServeGRPC(t, func(s *grpc.Server) {
		actionpb.RegisterActionServiceServer(s, identityActions{identity: recipient.Identity})
	})
	ca := environmentruntimetransporttesting.NewCA(t)
	serving := ca.IssueWithUsage(t, environment_runtime_transport.NotificationsURI, x509.ExtKeyUsageServerAuth)
	service := start(t, func(c *config) {
		c.RPC.TLS = &listener.TLS{CertFile: serving.CertFile, KeyFile: serving.KeyFile}
		c.RPC.Auth = append(c.RPC.Auth, &listener.SPIFFEAuth{CAFile: serving.CAFile})
		c.SessionDispatcher.Target, c.HistoryService.Target, c.Actions.URL = dispatcherAddress, historyAddress, "http://"+actionAddress
	})
	set := &policyapi.PolicySet{
		ObjectMeta: metav1.ObjectMeta{Namespace: testNamespace, Name: sender.Identity.Name + "-send"},
		Spec: policyapi.PolicySetSpec{Rules: []policyapi.Rule{{
			Type: "session_inbox_access", Operations: []string{"send_message"},
			Targets: &policyapi.SandboxTargets{Sandboxes: []policyapi.SandboxTarget{{Name: recipient.Sandbox.Name}}},
		}}},
	}
	binding := &policyapi.PolicyBinding{
		ObjectMeta: metav1.ObjectMeta{Namespace: testNamespace, Name: sender.Identity.Name},
		Spec: policyapi.PolicyBindingSpec{
			Subject: policyapi.Subject{Identity: &policyapi.IdentityRef{Name: sender.Identity.Name}}, PolicySets: []string{set.Name},
		},
	}
	for _, object := range []client.Object{set, binding} {
		if err := apiClient.Create(t.Context(), object); err != nil {
			t.Fatal(err)
		}
		t.Cleanup(func() {
			if err := client.IgnoreNotFound(apiClient.Delete(context.Background(), object)); err != nil {
				t.Error(err)
			}
		})
	}
	eventually(t, "the binding to be ready", func() bool {
		var current policyapi.PolicyBinding
		if err := apiClient.Get(t.Context(), client.ObjectKeyFromObject(binding), &current); err != nil {
			t.Fatal(err)
		}
		return meta.IsStatusConditionTrue(current.Status.Conditions, "SessionInboxAccessReady")
	})
	connect := func(caller workloadauthtesting.Workload) notificationspb.NotificationsClient {
		credentials, err := ca.Issue(t, caller.URI).ClientCredentials(environment_runtime_transport.NotificationsURI)
		if err != nil {
			t.Fatal(err)
		}
		conn, err := grpc.NewClient(service.rpcAddress, grpc.WithTransportCredentials(credentials))
		if err != nil {
			t.Fatal(err)
		}
		t.Cleanup(func() { conn.Close() })
		return notificationspb.NewNotificationsClient(conn)
	}
	senderClient, recipientClient, strangerClient := connect(sender.Workload), connect(recipient.Workload), connect(stranger.Workload)
	ctx, cancel := context.WithTimeout(t.Context(), callDeadline)
	defer cancel()
	message := &notificationspb.SendMessageRequest{SessionId: testSession, IdempotencyKey: "review", Body: "Please review the change."}

	if _, err := strangerClient.SendMessage(ctx, message); status.Code(err) != codes.PermissionDenied {
		t.Fatalf("an Identity no binding names sent to another Identity's Session: %v", err)
	}
	// The recipient's own Identity sends to its Session with no binding: no binding names it.
	own, err := recipientClient.SendMessage(ctx, &notificationspb.SendMessageRequest{SessionId: testSession, IdempotencyKey: "note", Body: "Note to self."})
	if err != nil {
		t.Fatalf("a send between Sessions of one Identity: %v", err)
	}
	sent, err := senderClient.SendMessage(ctx, message)
	if err != nil {
		t.Fatalf("the granted Identity's send: %v", err)
	}
	if _, err := senderClient.GetMessage(ctx, &notificationspb.GetMessageRequest{MessageId: sent.GetMessage().GetId()}); err != nil {
		t.Fatalf("the sender follows its message: %v", err)
	}
	if _, err := recipientClient.GetMessage(ctx, &notificationspb.GetMessageRequest{MessageId: sent.GetMessage().GetId()}); status.Code(err) != codes.NotFound {
		t.Fatalf("the recipient follows the sender's message: %v", err)
	}
	read, err := recipientClient.ReadInbox(ctx, &notificationspb.ReadInboxRequest{SessionId: testSession})
	if err != nil || len(read.GetEntries()) != 2 {
		t.Fatalf("the recipient Identity's inbox = %v, %v; want both messages", read, err)
	}
	for _, entry := range read.GetEntries() {
		got := entry.GetEvent().GetMessage()
		from := sender
		if got.GetMessageId() == own.GetMessage().GetId() {
			from = recipient
		} else if got.GetMessageId() != sent.GetMessage().GetId() {
			t.Fatalf("inbox entry %v names neither message", entry)
		}
		want := &notificationspb.MessageEvent{
			MessageId:      got.GetMessageId(),
			Sender:         &identitypb.ServiceAccount{Namespace: testNamespace, Name: from.Sandbox.Name},
			SenderIdentity: &identitypb.Identity{Name: from.Identity.Name, Uid: string(from.Identity.UID)},
		}
		if !proto.Equal(got, want) {
			t.Fatalf("the recipient sees the message as from %v, want %v", got, want)
		}
	}
	if listed, err := senderClient.ListInboxes(ctx, &notificationspb.ListInboxesRequest{}); err != nil || len(listed.GetInboxes()) != 0 {
		t.Fatalf("the sender sees inboxes %v, %v; want none", listed, err)
	}

	subscribe := &notificationspb.CreateSubscriptionRequest{
		SessionId: testSession, IdempotencyKey: "follow",
		Source: &notificationspb.Source{Provider: &notificationspb.Source_Actions{Actions: &notificationspb.ActionsSource{RequestId: uuid.NewString()}}},
	}
	if _, err := senderClient.CreateSubscription(ctx, subscribe); status.Code(err) != codes.NotFound {
		t.Fatalf("an Identity subscribed another Identity's Session: %v", err)
	}
	if _, err := recipientClient.CreateSubscription(ctx, subscribe); err != nil {
		t.Fatalf("the recipient Identity subscribes its Session: %v", err)
	}
}
