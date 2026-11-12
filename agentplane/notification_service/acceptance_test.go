package main

import (
	"context"
	"encoding/json"
	"errors"
	"reflect"
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/identity/auth/auth"

	identitypb "github.com/agentydragon/ducktape/agentplane/proto/identity"

	actionpb "github.com/agentydragon/ducktape/agentplane/action_service/action_servicepb"
	historypb "github.com/agentydragon/ducktape/agentplane/history_service/history_servicepb"
	"github.com/agentydragon/ducktape/agentplane/notification_service/notificationspb"
	notificationtesting "github.com/agentydragon/ducktape/agentplane/notification_service/testing" // Shared gRPC fixtures; avoids colliding with Go testing.
	policyapi "github.com/agentydragon/ducktape/agentplane/policy/api/v1alpha1"
	commandpb "github.com/agentydragon/ducktape/agentplane/proto/command"
	eventpb "github.com/agentydragon/ducktape/agentplane/proto/event"
	eventlogpb "github.com/agentydragon/ducktape/agentplane/proto/event_log"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	dispatcherpb "github.com/agentydragon/ducktape/agentplane/runner_gateway/dispatcher/session_dispatcherpb"
	"github.com/google/uuid"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/encoding/protojson"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/timestamppb"

	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"

	"sigs.k8s.io/controller-runtime/pkg/client"
)

// ownedActions is an Action Service whose requests were each submitted by the ServiceAccount that owners names,
// and whose log holds a pending decision and then a denial.
type ownedActions struct {
	actionpb.UnimplementedActionServiceServer
	owners map[string]string
}

func (a ownedActions) GetActionRequest(ctx context.Context, request *actionpb.GetActionRequestRequest) (*actionpb.GetActionRequestResponse, error) {
	owner, known := a.owners[request.GetRequestId()]
	if !known {
		return nil, status.Error(codes.NotFound, "test-action-not-found")
	}
	account, err := kube.ServiceAccount(ctx, auth.Account{Namespace: testNamespace, Name: owner})
	if err != nil {
		return nil, err
	}
	uid := string(account.UID)
	return &actionpb.GetActionRequestResponse{ActionRequest: &actionpb.ActionRequest{
		CallerUid: &uid,
		Caller:    &identitypb.ServiceAccount{Namespace: testNamespace, Name: owner},
	}}, nil
}

func (a ownedActions) ListActionRequestEvents(_ context.Context, request *actionpb.ListActionRequestEventsRequest) (*actionpb.ListActionRequestEventsResponse, error) {
	if _, known := a.owners[request.GetRequestId()]; !known {
		return nil, status.Error(codes.NotFound, "test-action-not-found")
	}
	response := &actionpb.ListActionRequestEventsResponse{}
	for sequence, state := range []actionpb.ActionState{
		actionpb.ActionState_ACTION_STATE_DECISION_PENDING,
		actionpb.ActionState_ACTION_STATE_DENIED,
	} {
		sequence++
		if int32(sequence) > request.GetAfterSequence() && int32(len(response.Events)) < request.GetLimit() {
			response.Events = append(response.Events, &actionpb.ActionEvent{
				Sequence: int32(sequence), State: state, At: timestamppb.New(time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC)),
			})
		}
	}
	return response, nil
}

// command is what a Dispatcher was asked to deliver.
type command struct{ sandbox, session, id, text string }

// noticeHint is all a notice tells its Session: the inbox to read, and how far it was acknowledged and filled when
// the notice was prepared.
type noticeHint struct {
	InboxID                   string `json:"inbox_id"`
	AcknowledgedAtPreparation int64  `json:"acknowledged_at_preparation"`
	ThroughAtPreparation      int64  `json:"through_at_preparation"`
}

// hint decodes a notice's text, failing the test on anything the hint does not hold.
func hint(t *testing.T, text string) noticeHint {
	t.Helper()
	decoder := json.NewDecoder(strings.NewReader(strings.TrimPrefix(text, "Agentplane inbox notice: ")))
	decoder.DisallowUnknownFields()
	var decoded noticeHint
	if err := decoder.Decode(&decoded); err != nil {
		t.Fatalf("notice %q: %v", text, err)
	}
	return decoded
}

// losingDispatcher stores every command it is sent, and tells the caller of the first that the call failed: the
// command is committed and its response is lost.
type losingDispatcher struct {
	openingDispatcher
	mu        sync.Mutex
	submitted []command
}

type openingDispatcher struct {
	dispatcherpb.UnimplementedSessionDispatcherServer
}

func (openingDispatcher) GetSession(ctx context.Context, request *dispatcherpb.GetSessionRequest) (*dispatcherpb.GetSessionResponse, error) {
	if request.GetSessionId() != testSession {
		return nil, status.Error(codes.NotFound, "opening not found")
	}
	account, err := kube.ServiceAccount(ctx, auth.Account{Namespace: testNamespace, Name: sandboxName})
	if err != nil {
		return nil, err
	}
	return &dispatcherpb.GetSessionResponse{Session: &dispatcherpb.Session{
		ExecutionIdentity: &identitypb.ServiceAccountIdentity{Account: &identitypb.ServiceAccount{Namespace: testNamespace, Name: sandboxName}, Uid: string(account.UID)},
		SessionId:         testSession, Placement: &sessionpb.SessionPlacement{Kind: &sessionpb.SessionPlacement_EnvironmentHosted{EnvironmentHosted: &sessionpb.EnvironmentHostedPlacement{Environment: &sessionpb.ExactEnvironment{Name: sandboxName, Uid: sandboxUID}}}},
	}}, nil
}

func (d *losingDispatcher) SubmitInput(_ context.Context, in *dispatcherpb.SubmitInputRequest) (*dispatcherpb.SubmitInputResponse, error) {
	d.mu.Lock()
	defer d.mu.Unlock()
	d.submitted = append(d.submitted, command{sandboxName, in.GetSessionId(), in.GetCommandId(), in.GetText()})
	if len(d.submitted) == 1 {
		return nil, status.Error(codes.Unavailable, "test-lost-response")
	}
	return &dispatcherpb.SubmitInputResponse{Dispatch: &dispatcherpb.Dispatch{
		CommandId: in.GetCommandId(), State: dispatcherpb.DeliveryState_DELIVERY_STATE_QUEUED_IN_DISPATCHER,
	}}, nil
}

func (d *losingDispatcher) commands() []command {
	d.mu.Lock()
	defer d.mu.Unlock()
	return append([]command(nil), d.submitted...)
}

// journal is the History of the test Sandbox's one Session: the runner entries a test adds.
type journal struct {
	historypb.UnimplementedHistoryServiceServer
	mu      sync.Mutex
	entries []*eventlogpb.EventEntry
}

func (j *journal) ReadSessionEvents(_ context.Context, in *historypb.ReadSessionEventsRequest) (*historypb.ReadSessionEventsResponse, error) {
	j.mu.Lock()
	defer j.mu.Unlock()
	response := &historypb.ReadSessionEventsResponse{}
	for _, entry := range j.entries {
		response.LastCursor = max(response.LastCursor, entry.GetCursor())
		if entry.GetCursor() > in.GetAfterCursor() && (in.GetLimit() == 0 || len(response.Entries) < int(in.GetLimit())) {
			response.Entries = append(response.Entries, entry)
		}
	}
	return response, nil
}

func (j *journal) GetSessionSummary(_ context.Context, in *historypb.GetSessionSummaryRequest) (*historypb.SessionSummary, error) {
	if in.GetSessionId() != testSession {
		return nil, status.Error(codes.NotFound, "test-unrecorded-session")
	}
	uid := sandboxUID
	return &historypb.SessionSummary{
		SessionId: testSession, Sandbox: &historypb.SandboxRef{Namespace: testNamespace, Name: sandboxName, Uid: &uid},
	}, nil
}

func (j *journal) add(entries ...*eventlogpb.EventEntry) {
	j.mu.Lock()
	defer j.mu.Unlock()
	j.entries = append(j.entries, entries...)
}

// agentGRPC calls a workload RPC as the given ServiceAccount and returns the protobuf JSON view used by the CLI.
func agentGRPC(t *testing.T, token string, call func(context.Context) (proto.Message, error)) (codes.Code, map[string]any) {
	t.Helper()
	response, err := call(bearing(t, token))
	if err != nil {
		return status.Code(err), nil
	}
	encoded, err := (protojson.MarshalOptions{EmitUnpopulated: true}).Marshal(response)
	if err != nil {
		t.Fatal(err)
	}
	var decoded map[string]any
	if err := json.Unmarshal(encoded, &decoded); err != nil {
		t.Fatal(err)
	}
	return codes.OK, decoded
}

// at follows object keys and list indexes into decoded JSON.
func at(t *testing.T, document any, path ...any) any {
	t.Helper()
	for _, step := range path {
		switch step := step.(type) {
		case string:
			object, _ := document.(map[string]any)
			value, present := object[step]
			if !present {
				t.Fatalf("no key %q on the way to %v in %v", step, path, document)
			}
			document = value
		case int:
			list, ok := document.([]any)
			if !ok || step >= len(list) {
				t.Fatalf("no element %d on the way to %v in %v", step, path, document)
			}
			document = list[step]
		}
	}
	return document
}

// eventually runs poll until it reports true, and fails the test if it does not within callDeadline.
func eventually(t *testing.T, what string, poll func() bool) {
	t.Helper()
	ticker := time.NewTicker(20 * time.Millisecond)
	defer ticker.Stop()
	expired := time.After(callDeadline)
	for !poll() {
		select {
		case <-ticker.C:
		case <-expired:
			t.Fatalf("timed out waiting for %s", what)
		case <-t.Context().Done():
			t.Fatalf("test ended while waiting for %s", what)
		}
	}
}

// wake makes the inbox due now, as the passing of its retry delay does. An inbox a worker holds stays its own until
// it releases it.
func wake(t *testing.T, service running) {
	t.Helper()
	for _, query := range []string{
		`UPDATE inbox SET next_attempt = now()`,
		`SELECT pg_notify('agentplane_notification_work', '')`,
	} {
		if _, err := service.db.ExecContext(t.Context(), query); err != nil {
			t.Fatal(err)
		}
	}
}

func runnerEntry(cursor uint64, observation *eventpb.Event) *eventlogpb.EventEntry {
	return &eventlogpb.EventEntry{Cursor: cursor, Event: observation}
}

// An agent follows an Action through gRPC: the notice is submitted again after the response to its first
// submission is lost, a new process finishes the delivery from the rows alone, and the agent reads and acknowledges
// the entries.
func TestAnAgentIsNotifiedOfAnActionAndAcknowledgesItsEntriesAfterALostResponseAndARestart(t *testing.T) {
	mine, theirs := uuid.NewString(), uuid.NewString()
	actionService := notificationtesting.ServeGRPC(t, func(server *grpc.Server) {
		actionpb.RegisterActionServiceServer(server, ownedActions{owners: map[string]string{mine: sandboxName, theirs: otherName}})
	})
	dispatcher, history := &losingDispatcher{}, &journal{}
	historyAddress := notificationtesting.ServeGRPC(t, func(s *grpc.Server) { historypb.RegisterHistoryServiceServer(s, history) })
	dispatcherAddress := notificationtesting.ServeGRPC(t, func(s *grpc.Server) { dispatcherpb.RegisterSessionDispatcherServer(s, dispatcher) })
	peers := func(c *config) {
		c.Actions.URL = "http://" + actionService
		c.HistoryService.Target, c.SessionDispatcher.Target = historyAddress, dispatcherAddress
		c.NoticeDebounce.QuietSeconds = 0
	}
	service := start(t, peers)

	subscribeTo := func(request string) *notificationspb.CreateSubscriptionRequest {
		return &notificationspb.CreateSubscriptionRequest{
			SessionId: testSession, IdempotencyKey: "listen",
			Source: &notificationspb.Source{Provider: &notificationspb.Source_Actions{Actions: &notificationspb.ActionsSource{RequestId: request}}},
		}
	}
	// The Sandbox is the owner's alone, and so is the Action request, though the service can read every request.
	if code, _ := agentGRPC(t, otherToken, func(ctx context.Context) (proto.Message, error) {
		return service.notifications.CreateSubscription(ctx, subscribeTo(mine))
	}); code != codes.NotFound {
		t.Errorf("another account subscribing to the Sandbox = %s, want NOT_FOUND", code)
	}
	if code, _ := agentGRPC(t, sandboxToken, func(ctx context.Context) (proto.Message, error) {
		return service.notifications.CreateSubscription(ctx, subscribeTo(theirs))
	}); code != codes.PermissionDenied {
		t.Errorf("subscribing to another account's Action request = %s, want PERMISSION_DENIED", code)
	}
	code, created := agentGRPC(t, sandboxToken, func(ctx context.Context) (proto.Message, error) {
		return service.notifications.CreateSubscription(ctx, subscribeTo(mine))
	})
	if code != codes.OK {
		t.Fatalf("subscribe = %s %v", code, created)
	}
	subscription := at(t, created, "subscription").(map[string]any)
	if subscription["idempotencyKey"] != "listen" || at(t, subscription, "source", "actions", "requestId") != mine ||
		at(t, subscription, "source", "actions", "afterSequence") != float64(0) {
		t.Errorf("subscription = %v", subscription)
	}
	// A subscription without GitHub observations says so, rather than leaving the key out.
	if github, present := subscription["github"]; !present || github != nil {
		t.Errorf("github = %v, present %v; want null", github, present)
	}
	_, replayed := agentGRPC(t, sandboxToken, func(ctx context.Context) (proto.Message, error) {
		return service.notifications.CreateSubscription(ctx, subscribeTo(mine))
	})
	if at(t, replayed, "subscription", "id") != subscription["id"] || at(t, replayed, "subscription", "inboxId") != subscription["inboxId"] {
		t.Errorf("an identical retry = %v, want the subscription %v", replayed, subscription)
	}
	inboxID := subscription["inboxId"].(string)

	// The service reads the request's events, prepares the notice and submits it; the Dispatcher commits it and the
	// response is lost, so the service submits the same command again.
	eventually(t, "the notice to be submitted twice", func() bool {
		wake(t, service)
		return len(dispatcher.commands()) >= 2
	})
	submitted := dispatcher.commands()
	for _, again := range submitted[1:] {
		if again != submitted[0] {
			t.Fatalf("a retry submitted %+v, want the command of the first attempt %+v", again, submitted[0])
		}
	}
	if submitted[0].sandbox != sandboxName || submitted[0].session != testSession {
		t.Errorf("the notice went to %q/%q", submitted[0].sandbox, submitted[0].session)
	}
	if got := hint(t, submitted[0].text); got != (noticeHint{InboxID: inboxID, ThroughAtPreparation: 2}) {
		t.Errorf("notice %+v, want a hint naming inbox %s and two entries", got, inboxID)
	}

	// A new process has only the rows to go on. The runner admits the command and the harness confirms it.
	if err := service.stop(); !errors.Is(err, context.Canceled) {
		t.Fatalf("the first process stopped with %v", err)
	}
	service = restart(t, peers)
	history.add(
		runnerEntry(1, &eventpb.Event{Observation: &eventpb.Event_CommandAdmitted{CommandAdmitted: &eventpb.CommandAdmitted{
			Command: &commandpb.Command{
				CommandId: submitted[0].id, Operation: &commandpb.Command_SubmitInput{SubmitInput: &commandpb.SubmitInput{Text: submitted[0].text}},
			},
		}}}),
		runnerEntry(2, &eventpb.Event{Observation: &eventpb.Event_HarnessUserMessageConfirmed{
			HarnessUserMessageConfirmed: &eventpb.HarnessUserMessageConfirmed{OriginCommandIds: []string{submitted[0].id}},
		}}),
	)
	var page map[string]any
	readInbox := func(after int64, token string) (codes.Code, map[string]any) {
		limit := int32(128)
		return agentGRPC(t, token, func(ctx context.Context) (proto.Message, error) {
			return service.notifications.ReadInbox(ctx, &notificationspb.ReadInboxRequest{SessionId: testSession, AfterCursor: after, Limit: &limit})
		})
	}
	eventually(t, "the notice to be confirmed", func() bool {
		wake(t, service)
		code, read := readInbox(0, sandboxToken)
		if code != codes.OK {
			t.Fatalf("read inbox = %s %v", code, read)
		}
		page = read
		return at(t, read, "notice", "confirmed") == true
	})
	if at(t, page, "notice", "commandId") != submitted[0].id {
		t.Errorf("the confirmed notice is command %v, want %s", at(t, page, "notice", "commandId"), submitted[0].id)
	}
	if at(t, page, "inbox", "acknowledged") != "0" {
		t.Errorf("reading acknowledged entries through %v, want none", at(t, page, "inbox", "acknowledged"))
	}
	var states []any
	for i, entry := range at(t, page, "entries").([]any) {
		states = append(states, at(t, entry, "payload", "state"))
		if at(t, entry, "event", "actions", "requestId") != mine || at(t, entry, "event", "actions", "sequence") != float64(i+1) {
			t.Errorf("entry %d is event %v", i, at(t, entry, "event"))
		}
	}
	if !reflect.DeepEqual(states, []any{"decision_pending", "denied"}) {
		t.Errorf("entry states = %v, want the Action's two states", states)
	}
	if code, _ := readInbox(0, otherToken); code != codes.NotFound {
		t.Errorf("another account reading the inbox = %s, want NOT_FOUND", code)
	}

	// Acknowledging moves the handled prefix only.
	acknowledge := func(through any) any {
		cursor, err := strconv.ParseInt(through.(string), 10, 64)
		if err != nil {
			t.Fatal(err)
		}
		code, acknowledged := agentGRPC(t, sandboxToken, func(ctx context.Context) (proto.Message, error) {
			return service.notifications.AcknowledgeInbox(ctx, &notificationspb.AcknowledgeInboxRequest{SessionId: testSession, ThroughCursor: cursor})
		})
		if code != codes.OK {
			t.Fatalf("acknowledge through %v = %s %v", through, code, acknowledged)
		}
		return at(t, acknowledged, "inbox", "acknowledged")
	}
	first, second := at(t, page, "entries", 0, "cursor"), at(t, page, "entries", 1, "cursor")
	if got := acknowledge(first); got != first {
		t.Errorf("acknowledged = %v after acknowledging through %v", got, first)
	}
	after, err := strconv.ParseInt(first.(string), 10, 64)
	if err != nil {
		t.Fatal(err)
	}
	_, remaining := readInbox(after, sandboxToken)
	if got := at(t, remaining, "entries"); len(got.([]any)) != 1 || at(t, got, 0, "cursor") != second {
		t.Errorf("entries after %v = %v, want only %v", first, got, second)
	}
	if got := acknowledge(second); got != second {
		t.Errorf("acknowledged = %v after acknowledging through %v", got, second)
	}
}

// grantSend binds a set allowing the account to send to the test Sandbox, and waits for the service to report
// the binding ready. The test deletes the binding when it ends, or earlier through the returned object.
func grantSend(t *testing.T, account string) *policyapi.PolicyBinding {
	t.Helper()
	set := &policyapi.PolicySet{
		ObjectMeta: metav1.ObjectMeta{Namespace: testNamespace, Name: account + "-send"},
		Spec: policyapi.PolicySetSpec{Rules: []policyapi.Rule{{
			Type: "session_inbox_access", Operations: []string{"send_message"},
			Targets: &policyapi.SandboxTargets{Sandboxes: []policyapi.SandboxTarget{{Name: sandboxName}}},
		}}},
	}
	binding := &policyapi.PolicyBinding{
		ObjectMeta: metav1.ObjectMeta{Namespace: testNamespace, Name: account},
		Spec: policyapi.PolicyBindingSpec{
			Subject:    policyapi.Subject{Namespace: testNamespace, Name: account},
			PolicySets: []string{set.Name},
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
	return binding
}

// An agent messages another Sandbox's Session over gRPC under a PolicyBinding the service watches:
// the recipient is notified and acknowledges the message, the sender follows it to the acknowledgement, and once
// the binding is gone the sender may send no more, while what it sent before stays the recipient's to read.
func TestAnAgentMessagesASessionItIsGrantedAndFollowsTheMessageToItsAcknowledgement(t *testing.T) {
	dispatcher, history := &losingDispatcher{}, &journal{}
	historyAddress := notificationtesting.ServeGRPC(t, func(s *grpc.Server) { historypb.RegisterHistoryServiceServer(s, history) })
	dispatcherAddress := notificationtesting.ServeGRPC(t, func(s *grpc.Server) { dispatcherpb.RegisterSessionDispatcherServer(s, dispatcher) })
	service := start(t, func(c *config) {
		c.HistoryService.Target, c.SessionDispatcher.Target = historyAddress, dispatcherAddress
		c.NoticeDebounce.QuietSeconds = 0
	})
	message := func(key string) *notificationspb.SendMessageRequest {
		return &notificationspb.SendMessageRequest{
			SessionId: testSession, IdempotencyKey: key, Body: "Please review the change.",
			FromSession: proto.String("test-from-session"),
		}
	}
	binding := grantSend(t, otherName)
	if code, refused := agentGRPC(t, sandboxToken, func(ctx context.Context) (proto.Message, error) {
		return service.notifications.SendMessage(ctx, message("ungranted"))
	}); code != codes.PermissionDenied {
		t.Fatalf("a send without binding grants = %s %v, want PERMISSION_DENIED", code, refused)
	}
	code, sent := agentGRPC(t, otherToken, func(ctx context.Context) (proto.Message, error) {
		return service.notifications.SendMessage(ctx, message("review"))
	})
	if code != codes.OK {
		t.Fatalf("send = %s %v", code, sent)
	}
	id := at(t, sent, "message", "id").(string)
	if at(t, sent, "message", "destinationRef", "uid") != sandboxUID {
		t.Errorf("the message went to %v, want the Sandbox's current incarnation", at(t, sent, "message", "destinationRef"))
	}

	// The recipient Session is told of its inbox and nothing more: neither the notice nor its retry after the lost
	// response carries the message.
	eventually(t, "the notice to be submitted twice", func() bool {
		wake(t, service)
		return len(dispatcher.commands()) >= 2
	})
	_, inboxes := agentGRPC(t, sandboxToken, func(ctx context.Context) (proto.Message, error) {
		return service.notifications.ListInboxes(ctx, &notificationspb.ListInboxesRequest{})
	})
	inboxID := at(t, inboxes, "inboxes", 0, "id").(string)
	for _, notice := range dispatcher.commands() {
		if notice.sandbox != sandboxName || notice.session != testSession || hint(t, notice.text).InboxID != inboxID {
			t.Errorf("notice %+v, want one naming inbox %s to %q/%q", notice, inboxID, sandboxName, testSession)
		}
	}
	receipt := func(id string) map[string]any {
		code, followed := agentGRPC(t, otherToken, func(ctx context.Context) (proto.Message, error) {
			return service.notifications.GetMessage(ctx, &notificationspb.GetMessageRequest{MessageId: id})
		})
		if code != codes.OK {
			t.Fatalf("get message = %s %v", code, followed)
		}
		return at(t, followed, "message").(map[string]any)
	}
	if got := receipt(id); got["covered"] != true || got["acknowledged"] != false {
		t.Errorf("once notified: %v", got)
	}
	entries := func(afterCursor string) map[string]any {
		after, err := strconv.ParseInt(afterCursor, 10, 64)
		if err != nil {
			t.Fatal(err)
		}
		limit := int32(128)
		_, page := agentGRPC(t, sandboxToken, func(ctx context.Context) (proto.Message, error) {
			return service.notifications.ReadInbox(ctx, &notificationspb.ReadInboxRequest{SessionId: testSession, AfterCursor: after, Limit: &limit})
		})
		return at(t, page, "entries", 0).(map[string]any)
	}
	acknowledge := func(token string, through any) codes.Code {
		cursor, err := strconv.ParseInt(through.(string), 10, 64)
		if err != nil {
			t.Fatal(err)
		}
		code, _ := agentGRPC(t, token, func(ctx context.Context) (proto.Message, error) {
			return service.notifications.AcknowledgeInbox(ctx, &notificationspb.AcknowledgeInboxRequest{SessionId: testSession, ThroughCursor: cursor})
		})
		return code
	}
	entry := entries("0")
	if at(t, entry, "event", "message", "messageId") != id || at(t, entry, "event", "message", "sender", "name") != otherName ||
		at(t, entry, "payload", "body") != "Please review the change." || at(t, entry, "payload", "from_session") != "test-from-session" {
		t.Errorf("entry = %v", entry)
	}
	// The inbox is the recipient's alone: the sender can neither read nor acknowledge it.
	if code, _ := agentGRPC(t, otherToken, func(ctx context.Context) (proto.Message, error) {
		limit := int32(128)
		return service.notifications.ReadInbox(ctx, &notificationspb.ReadInboxRequest{SessionId: testSession, Limit: &limit})
	}); code != codes.NotFound {
		t.Errorf("the sender reading the recipient's inbox = %s, want NOT_FOUND", code)
	}
	if code := acknowledge(otherToken, entry["cursor"]); code != codes.NotFound {
		t.Errorf("the sender acknowledging the recipient's inbox = %s, want NOT_FOUND", code)
	}
	if code := acknowledge(sandboxToken, entry["cursor"]); code != codes.OK {
		t.Fatalf("acknowledge = %s", code)
	}
	if got := receipt(id); got["acknowledged"] != true {
		t.Errorf("once acknowledged: %v", got)
	}

	code, sent = agentGRPC(t, otherToken, func(ctx context.Context) (proto.Message, error) {
		return service.notifications.SendMessage(ctx, message("follow-up"))
	})
	if code != codes.OK {
		t.Fatalf("send = %s %v", code, sent)
	}
	followUp := at(t, sent, "message", "id").(string)
	if err := apiClient.Delete(t.Context(), binding); err != nil {
		t.Fatal(err)
	}
	eventually(t, "sends to be refused", func() bool {
		code, _ := agentGRPC(t, otherToken, func(ctx context.Context) (proto.Message, error) {
			return service.notifications.SendMessage(ctx, message("after-revocation"))
		})
		return code == codes.PermissionDenied
	})
	if unread := entries(entry["cursor"].(string)); at(t, unread, "event", "message", "messageId") != followUp {
		t.Fatalf("after the revocation the inbox holds %v, want the unread follow-up", unread)
	} else if code := acknowledge(sandboxToken, unread["cursor"]); code != codes.OK {
		t.Fatalf("acknowledge the follow-up = %d", code)
	}
	if got := receipt(followUp); got["acknowledged"] != true {
		t.Errorf("the follow-up once acknowledged: %v", got)
	}
}
