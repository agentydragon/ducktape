package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"reflect"
	"strings"
	"sync"
	"testing"
	"time"

	actionpb "github.com/agentydragon/ducktape/agentplane/action_service/action_servicepb"
	historypb "github.com/agentydragon/ducktape/agentplane/history_service/history_servicepb"
	policyapi "github.com/agentydragon/ducktape/agentplane/policy/api/v1alpha1"
	commandpb "github.com/agentydragon/ducktape/agentplane/proto/command"
	eventpb "github.com/agentydragon/ducktape/agentplane/proto/event"
	eventlogpb "github.com/agentydragon/ducktape/agentplane/proto/event_log"
	sandboxespb "github.com/agentydragon/ducktape/agentplane/proto/sandboxes"
	dispatcherpb "github.com/agentydragon/ducktape/agentplane/runner_gateway/dispatcher/session_dispatcherpb"
	"github.com/google/uuid"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
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

func (a ownedActions) GetActionRequest(_ context.Context, request *actionpb.GetActionRequestRequest) (*actionpb.GetActionRequestResponse, error) {
	owner, known := a.owners[request.GetRequestId()]
	if !known {
		return nil, status.Error(codes.NotFound, "test-action-not-found")
	}
	return &actionpb.GetActionRequestResponse{ActionRequest: &actionpb.ActionRequest{
		Caller: &sandboxespb.ServiceAccount{Namespace: testNamespace, Name: owner},
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
	dispatcherpb.UnimplementedSessionDispatcherServer
	mu        sync.Mutex
	submitted []command
}

func (d *losingDispatcher) SubmitInput(_ context.Context, in *dispatcherpb.SubmitInputRequest) (*dispatcherpb.SubmitInputResponse, error) {
	d.mu.Lock()
	defer d.mu.Unlock()
	d.submitted = append(d.submitted, command{in.GetSandbox(), in.GetSessionId(), in.GetCommandId(), in.GetText()})
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
	return &historypb.SessionSummary{
		SessionId: testSession, Sandbox: &historypb.SandboxRef{Namespace: testNamespace, Name: sandboxName},
	}, nil
}

func (j *journal) add(entries ...*eventlogpb.EventEntry) {
	j.mu.Lock()
	defer j.mu.Unlock()
	j.entries = append(j.entries, entries...)
}

// agentCall is an agent's HTTP request, answered with the JSON the service sent.
func agentCall(t *testing.T, service running, method, path, token string, body any) (int, map[string]any) {
	t.Helper()
	var payload io.Reader
	if body != nil {
		encoded, err := json.Marshal(body)
		if err != nil {
			t.Fatal(err)
		}
		payload = bytes.NewReader(encoded)
	}
	request, err := http.NewRequestWithContext(t.Context(), method, service.url+path, payload)
	if err != nil {
		t.Fatal(err)
	}
	request.Header.Set("Authorization", "Bearer "+token)
	if body != nil {
		request.Header.Set("Content-Type", "application/json")
	}
	response, err := (&http.Client{Timeout: callDeadline}).Do(request)
	if err != nil {
		t.Fatal(err)
	}
	defer response.Body.Close()
	var decoded map[string]any
	if err := json.NewDecoder(response.Body).Decode(&decoded); err != nil {
		t.Fatalf("%s %s answered %d: %v", method, path, response.StatusCode, err)
	}
	return response.StatusCode, decoded
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

// An agent follows an Action through the HTTP routes: the notice is submitted again after the response to its first
// submission is lost, a new process finishes the delivery from the rows alone, and the agent reads and acknowledges
// the entries.
func TestAnAgentIsNotifiedOfAnActionAndAcknowledgesItsEntriesAfterALostResponseAndARestart(t *testing.T) {
	mine, theirs := uuid.NewString(), uuid.NewString()
	actionService := serveGRPC(t, func(server *grpc.Server) {
		actionpb.RegisterActionServiceServer(server, ownedActions{owners: map[string]string{mine: sandboxName, theirs: otherName}})
	})
	dispatcher, history := &losingDispatcher{}, &journal{}
	historyAddress := serveGRPC(t, func(s *grpc.Server) { historypb.RegisterHistoryServiceServer(s, history) })
	dispatcherAddress := serveGRPC(t, func(s *grpc.Server) { dispatcherpb.RegisterSessionDispatcherServer(s, dispatcher) })
	peers := func(c *config) {
		c.Actions.URL = "http://" + actionService
		c.HistoryService.Target, c.SessionDispatcher.Target = historyAddress, dispatcherAddress
		c.NoticeDebounce.QuietSeconds = 0
	}
	service := start(t, peers)

	subscribeTo := func(request string) map[string]any {
		return map[string]any{
			"destinationRef": map[string]any{"namespace": testNamespace, "name": sandboxName, "uid": sandboxUID},
			"sessionId":      testSession,
			"idempotencyKey": "listen",
			"source":         map[string]any{"actions": map[string]any{"requestId": request}},
		}
	}
	// The Sandbox is the owner's alone, and so is the Action request, though the service can read every request.
	if code, _ := agentCall(t, service, http.MethodPost, "/v1/subscriptions", otherToken, subscribeTo(mine)); code != http.StatusNotFound {
		t.Errorf("another account subscribing to the Sandbox = %d, want 404", code)
	}
	if code, _ := agentCall(t, service, http.MethodPost, "/v1/subscriptions", sandboxToken, subscribeTo(theirs)); code != http.StatusForbidden {
		t.Errorf("subscribing to another account's Action request = %d, want 403", code)
	}
	code, created := agentCall(t, service, http.MethodPost, "/v1/subscriptions", sandboxToken, subscribeTo(mine))
	if code != http.StatusOK {
		t.Fatalf("subscribe = %d %v", code, created)
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
	_, replayed := agentCall(t, service, http.MethodPost, "/v1/subscriptions", sandboxToken, subscribeTo(mine))
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
	entries := "/v1/inboxes/" + inboxID + "/entries?limit=128&after_cursor="
	var page map[string]any
	eventually(t, "the notice to be confirmed", func() bool {
		wake(t, service)
		code, read := agentCall(t, service, http.MethodGet, entries+"0", sandboxToken, nil)
		if code != http.StatusOK {
			t.Fatalf("read inbox = %d %v", code, read)
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
	if code, _ := agentCall(t, service, http.MethodGet, entries+"0", otherToken, nil); code != http.StatusNotFound {
		t.Errorf("another account reading the inbox = %d, want 404", code)
	}

	// Acknowledging moves the handled prefix only.
	acknowledge := func(through any) any {
		code, acknowledged := agentCall(t, service, http.MethodPut, "/v1/inboxes/"+inboxID+"/acknowledgement", sandboxToken,
			map[string]any{"throughCursor": through})
		if code != http.StatusOK {
			t.Fatalf("acknowledge through %v = %d %v", through, code, acknowledged)
		}
		return at(t, acknowledged, "inbox", "acknowledged")
	}
	first, second := at(t, page, "entries", 0, "cursor"), at(t, page, "entries", 1, "cursor")
	if got := acknowledge(first); got != first {
		t.Errorf("acknowledged = %v after acknowledging through %v", got, first)
	}
	_, remaining := agentCall(t, service, http.MethodGet, entries+first.(string), sandboxToken, nil)
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
			Subject:    policyapi.ServiceAccountRef{Namespace: testNamespace, Name: account},
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

// An agent messages another Sandbox's Session through the HTTP routes under a PolicyBinding the service watches:
// the recipient is notified and acknowledges the message, the sender follows it to the acknowledgement, and once
// the binding is gone the sender may send no more, while what it sent before stays the recipient's to read.
func TestAnAgentMessagesASessionItIsGrantedAndFollowsTheMessageToItsAcknowledgement(t *testing.T) {
	dispatcher, history := &losingDispatcher{}, &journal{}
	historyAddress := serveGRPC(t, func(s *grpc.Server) { historypb.RegisterHistoryServiceServer(s, history) })
	dispatcherAddress := serveGRPC(t, func(s *grpc.Server) { dispatcherpb.RegisterSessionDispatcherServer(s, dispatcher) })
	service := start(t, func(c *config) {
		c.HistoryService.Target, c.SessionDispatcher.Target = historyAddress, dispatcherAddress
		c.NoticeDebounce.QuietSeconds = 0
	})
	message := func(key string) map[string]any {
		return map[string]any{
			"destinationRef": map[string]any{"namespace": testNamespace, "name": sandboxName},
			"sessionId":      testSession, "idempotencyKey": key, "body": "Please review the change.",
			"fromSession": "test-from-session",
		}
	}
	binding := grantSend(t, otherName)
	if code, refused := agentCall(t, service, http.MethodPost, "/v1/messages", sandboxToken, message("ungranted")); code != http.StatusForbidden {
		t.Fatalf("a send no binding grants = %d %v, want 403", code, refused)
	}
	code, sent := agentCall(t, service, http.MethodPost, "/v1/messages", otherToken, message("review"))
	if code != http.StatusOK {
		t.Fatalf("send = %d %v", code, sent)
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
	_, inboxes := agentCall(t, service, http.MethodGet, "/v1/inboxes", sandboxToken, nil)
	inboxID := at(t, inboxes, "inboxes", 0, "id").(string)
	for _, notice := range dispatcher.commands() {
		if notice.sandbox != sandboxName || notice.session != testSession || hint(t, notice.text).InboxID != inboxID {
			t.Errorf("notice %+v, want one naming inbox %s to %q/%q", notice, inboxID, sandboxName, testSession)
		}
	}
	receipt := func(id string) map[string]any {
		code, followed := agentCall(t, service, http.MethodGet, "/v1/messages/"+id, otherToken, nil)
		if code != http.StatusOK {
			t.Fatalf("get message = %d %v", code, followed)
		}
		return at(t, followed, "message").(map[string]any)
	}
	if got := receipt(id); got["covered"] != true || got["acknowledged"] != false {
		t.Errorf("once notified: %v", got)
	}
	entries := func(afterCursor string) map[string]any {
		_, page := agentCall(t, service, http.MethodGet, "/v1/inboxes/"+inboxID+"/entries?limit=128&after_cursor="+afterCursor, sandboxToken, nil)
		return at(t, page, "entries", 0).(map[string]any)
	}
	acknowledge := func(token string, through any) int {
		code, _ := agentCall(t, service, http.MethodPut, "/v1/inboxes/"+inboxID+"/acknowledgement", token, map[string]any{"throughCursor": through})
		return code
	}
	entry := entries("0")
	if at(t, entry, "event", "message", "messageId") != id || at(t, entry, "event", "message", "sender", "name") != otherName ||
		at(t, entry, "payload", "body") != "Please review the change." || at(t, entry, "payload", "from_session") != "test-from-session" {
		t.Errorf("entry = %v", entry)
	}
	// The inbox is the recipient's alone: the sender can neither read nor acknowledge it.
	if code, _ := agentCall(t, service, http.MethodGet, "/v1/inboxes/"+inboxID+"/entries?limit=128&after_cursor=0", otherToken, nil); code != http.StatusNotFound {
		t.Errorf("the sender reading the recipient's inbox = %d, want 404", code)
	}
	if code := acknowledge(otherToken, entry["cursor"]); code != http.StatusNotFound {
		t.Errorf("the sender acknowledging the recipient's inbox = %d, want 404", code)
	}
	if code := acknowledge(sandboxToken, entry["cursor"]); code != http.StatusOK {
		t.Fatalf("acknowledge = %d", code)
	}
	if got := receipt(id); got["acknowledged"] != true {
		t.Errorf("once acknowledged: %v", got)
	}

	code, sent = agentCall(t, service, http.MethodPost, "/v1/messages", otherToken, message("follow-up"))
	if code != http.StatusOK {
		t.Fatalf("send = %d %v", code, sent)
	}
	followUp := at(t, sent, "message", "id").(string)
	if err := apiClient.Delete(t.Context(), binding); err != nil {
		t.Fatal(err)
	}
	eventually(t, "sends to be refused", func() bool {
		code, _ := agentCall(t, service, http.MethodPost, "/v1/messages", otherToken, message("after-revocation"))
		return code == http.StatusForbidden
	})
	if unread := entries(entry["cursor"].(string)); at(t, unread, "event", "message", "messageId") != followUp {
		t.Fatalf("after the revocation the inbox holds %v, want the unread follow-up", unread)
	} else if code := acknowledge(sandboxToken, unread["cursor"]); code != http.StatusOK {
		t.Fatalf("acknowledge the follow-up = %d", code)
	}
	if got := receipt(followUp); got["acknowledged"] != true {
		t.Errorf("the follow-up once acknowledged: %v", got)
	}
}
