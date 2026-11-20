package rpc

import (
	"context"
	"encoding/json"
	"net/http"
	"slices"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/action_service/api/contracts/action_servicepb"
	"github.com/agentydragon/ducktape/agentplane/action_service/api/contracts/operator_action_servicepb"
	"github.com/agentydragon/ducktape/agentplane/action_service/internal/store"
	"github.com/google/uuid"
	"google.golang.org/grpc/codes"
	"google.golang.org/protobuf/encoding/protojson"
)

type gmailSummariesFunc struct {
	// Embed unused preview methods so the test fails if it calls an unrelated resource.
	gmailPreviewProvider
	threads func(context.Context, []string) []*operator_action_servicepb.GmailThreadSummary
}

func (f gmailSummariesFunc) GmailThreadSummaries(ctx context.Context, ids []string) []*operator_action_servicepb.GmailThreadSummary {
	return f.threads(ctx, ids)
}

type tanaSummariesFuncs struct {
	nodes     func(context.Context, []string) []*operator_action_servicepb.TanaNodeSummary
	workspace func(context.Context, string) *operator_action_servicepb.TanaWorkspaceSummary
}

func (f tanaSummariesFuncs) TanaNodeSummaries(ctx context.Context, ids []string) []*operator_action_servicepb.TanaNodeSummary {
	return f.nodes(ctx, ids)
}

func (f tanaSummariesFuncs) TanaWorkspaceSummary(ctx context.Context, id string) *operator_action_servicepb.TanaWorkspaceSummary {
	return f.workspace(ctx, id)
}

func TestIntegrationSummariesUseOnlyRecordedIDs(t *testing.T) {
	gmailIDs := make(chan []string, 1)
	tanaIDs := make(chan []string, 2)
	previews := IntegrationPreviews{
		Gmail: gmailSummariesFunc{threads: func(_ context.Context, ids []string) []*operator_action_servicepb.GmailThreadSummary {
			select {
			case gmailIDs <- ids:
			default:
				t.Error("unexpected extra Gmail lookup")
			}
			return []*operator_action_servicepb.GmailThreadSummary{{ThreadId: "thread-1", Subject: "Deployment review"}}
		}},
		Tana: tanaSummariesFuncs{nodes: func(_ context.Context, ids []string) []*operator_action_servicepb.TanaNodeSummary {
			select {
			case tanaIDs <- ids:
			default:
				t.Error("unexpected extra Tana lookup")
			}
			return []*operator_action_servicepb.TanaNodeSummary{{NodeId: "target", Name: "Destination"}}
		}, workspace: func(_ context.Context, id string) *operator_action_servicepb.TanaWorkspaceSummary {
			if id != "workspace-123" {
				t.Errorf("looked up unexpected workspace %q", id)
			}
			return &operator_action_servicepb.TanaWorkspaceSummary{WorkspaceId: id, Name: "Product"}
		}},
	}
	namespace := newPolicyNamespace(t)
	s := serveWithPolicyOptions(t, newCatalog(t, submissionCatalog), namespace, namespace.watch(t), nil, previews)
	submit := func(group, name, args string) store.ActionRequest {
		request, err := s.store.Submit(t.Context(), s.caller, store.ActionRequestInput{
			IdempotencyKey: group + "/" + name, Action: store.ActionIdentity{Group: group, Name: name},
			Arguments: json.RawMessage(args), Title: name,
		}, store.Admission{RequestID: uuid.New()})
		if err != nil {
			t.Fatal(err)
		}
		return request
	}
	gmail := submit("gmail", "threads_modify_labels", `{"thread_ids":["thread-1","thread-1","../other","2","3","4","5","6","7","8","9"]}`)
	tana := submit("tana", "move_node", `{"nodeId":"draft","targetNodeId":"target","sourceParentId":"source","referenceNodeId":"draft"}`)
	gmailCall := &operator_action_servicepb.GetGmailThreadSummariesRequest{RequestId: gmail.ID.String()}
	_, err := s.operators.GetGmailThreadSummaries(as(t, callerCertificate), gmailCall)
	wantCode(t, "workload cannot read Gmail summaries", err, codes.Unauthenticated)
	tanaCall := &operator_action_servicepb.GetTanaNodeSummariesRequest{RequestId: tana.ID.String()}
	_, err = s.operators.GetTanaNodeSummaries(as(t, callerCertificate), tanaCall)
	wantCode(t, "workload cannot read Tana summaries", err, codes.Unauthenticated)
	_, err = s.operators.GetTanaNodeSummaries(as(t, s.operatorToken), &operator_action_servicepb.GetTanaNodeSummariesRequest{RequestId: gmail.ID.String()})
	wantCode(t, "the wrong integration receipt", err, codes.InvalidArgument)

	code, body := s.get(t, "/v1/operator/action-requests/"+gmail.ID.String()+"/gmail-thread-summaries", s.operatorToken)
	var gmailResponse operator_action_servicepb.GetGmailThreadSummariesResponse
	if code != http.StatusOK || protojson.Unmarshal(body, &gmailResponse) != nil ||
		len(gmailResponse.Threads) != 1 || gmailResponse.Threads[0].GetSubject() != "Deployment review" {
		t.Fatalf("Gmail summary HTTP response %d %s", code, body)
	}
	tanaResponse, err := s.operators.GetTanaNodeSummaries(as(t, s.operatorToken), tanaCall)
	if err != nil || len(tanaResponse.Nodes) != 1 || tanaResponse.Nodes[0].GetName() != "Destination" {
		t.Fatalf("Tana lookup: %v, %v", tanaResponse, err)
	}

	calendar := submit("tana", "get_or_create_calendar_node", `{"workspaceId":"workspace-123","nodeId":"unrelated"}`)
	calendarResponse, err := s.operators.GetTanaNodeSummaries(as(t, s.operatorToken), &operator_action_servicepb.GetTanaNodeSummariesRequest{RequestId: calendar.ID.String()})
	if err != nil || calendarResponse.GetWorkspace().GetName() != "Product" || len(calendarResponse.GetNodes()) != 0 {
		t.Fatalf("workspace lookup: %v, %v", calendarResponse, err)
	}

	invalid := submit("tana", "list_workspaces", `{"workspaceId":"../other"}`)
	response, err := s.operators.GetTanaNodeSummaries(as(t, s.operatorToken), &operator_action_servicepb.GetTanaNodeSummariesRequest{RequestId: invalid.ID.String()})
	wantCode(t, "unsupported Tana action", err, codes.InvalidArgument)
	if response != nil {
		t.Fatalf("unsupported action returned %v", response)
	}

	checkbox := submit("tana", "set_tag_checkbox", `{
		"tagId":"tag-1","nodeId":"unrelated",
		"doneStateMapping":{"fieldId":"status","checkedValues":["done"],"uncheckedValues":["active"]}
	}`)
	checkboxResponse, err := s.operators.GetTanaNodeSummaries(as(t, s.operatorToken), &operator_action_servicepb.GetTanaNodeSummariesRequest{RequestId: checkbox.ID.String()})
	if err != nil || len(checkboxResponse.GetNodes()) == 0 || checkboxResponse.GetWorkspace() != nil {
		t.Fatalf("checkbox lookup: %v, %v", checkboxResponse, err)
	}

	for _, tc := range []struct {
		ids     chan []string
		want    []string
		receipt store.ActionRequest
	}{
		{gmailIDs, []string{"thread-1", "2", "3", "4", "5", "6", "7", "8"}, gmail},
		{tanaIDs, []string{"draft", "target", "source"}, tana},
		{tanaIDs, []string{"tag-1", "status", "done", "active"}, checkbox},
	} {
		select {
		case got := <-tc.ids:
			if !slices.Equal(got, tc.want) {
				t.Errorf("looked up %q; want %q", got, tc.want)
			}
		default:
			t.Errorf("no lookup for %s", tc.receipt.Action.Group)
		}
		found, err := s.store.Get(t.Context(), tc.receipt.ID, operatorPrincipal(s.issuer))
		if err != nil || found.Version != tc.receipt.Version || found.Execution != nil || found.Decision != nil {
			t.Errorf("lookup changed %s receipt to %+v, %v", tc.receipt.Action.Group, found, err)
		}
	}
}

func TestGmailThreadSummariesForStoredReplyAndSearch(t *testing.T) {
	var lookedUp []string
	var mu sync.Mutex
	previews := IntegrationPreviews{
		Gmail: gmailSummariesFunc{threads: func(_ context.Context, ids []string) []*operator_action_servicepb.GmailThreadSummary {
			mu.Lock()
			defer mu.Unlock()
			lookedUp = append(lookedUp, ids...)
			return nil
		}},
	}
	namespace := newPolicyNamespace(t)
	s := serveWithPolicyOptions(t, newCatalog(t, submissionCatalog), namespace, namespace.watch(t), nil, previews)
	submit := func(name, args string) store.ActionRequest {
		t.Helper()
		request, err := s.store.Submit(t.Context(), s.caller, store.ActionRequestInput{
			IdempotencyKey: name, Action: store.ActionIdentity{Group: "gmail", Name: name},
			Arguments: json.RawMessage(args), Title: name,
		}, store.Admission{RequestID: uuid.New()})
		if err != nil {
			t.Fatal(err)
		}
		return request
	}
	lookup := func(request store.ActionRequest) {
		t.Helper()
		_, err := s.operators.GetGmailThreadSummaries(as(t, s.operatorToken), &operator_action_servicepb.GetGmailThreadSummariesRequest{RequestId: request.ID.String()})
		if err != nil {
			t.Fatal(err)
		}
	}
	lookup(submit("drafts_create", `{"thread_id":"reply-thread","thread_ids":["unrelated-thread"]}`))
	search := submit("threads_list", `{"thread_id":"unrelated-thread","q":"test"}`)
	lookup(search)
	s.decide(t, search, store.VerdictAllow)
	claim, err := s.store.ClaimExecution(t.Context(), search.ID, "test-executor", time.Minute, func(store.ActionIdentity) bool { return true })
	if err != nil || claim == nil {
		t.Fatalf("claim: %v, %v", claim, err)
	}
	if _, err := s.store.MarkRunning(t.Context(), search.ID); err != nil {
		t.Fatal(err)
	}
	if err := s.store.FinishExecution(t.Context(), search.ID, claim.Lease, store.ExecutionOutcome{
		State:  store.ExecutionSucceeded,
		Result: json.RawMessage(`{"content":[],"structuredContent":{"threads":[{"id":"returned-thread"},{"id":"returned-thread"},{"id":"../invalid"}]}}`),
	}); err != nil {
		t.Fatal(err)
	}
	lookup(search)
	mu.Lock()
	defer mu.Unlock()
	if want := []string{"reply-thread", "returned-thread"}; !slices.Equal(lookedUp, want) {
		t.Errorf("looked up %q; want %q", lookedUp, want)
	}
}

type calendarPreviewFunc func(context.Context, string, string) *operator_action_servicepb.GoogleCalendarEventPreview

func (f calendarPreviewFunc) GoogleCalendarEventPreview(ctx context.Context, calendarID, eventID string) *operator_action_servicepb.GoogleCalendarEventPreview {
	return f(ctx, calendarID, eventID)
}

func TestGoogleCalendarEventPreviewUsesOnlyPendingRecordedTargets(t *testing.T) {
	lookups := make(chan [2]string, 4)
	previews := IntegrationPreviews{GoogleCalendar: calendarPreviewFunc(func(_ context.Context, calendarID, eventID string) *operator_action_servicepb.GoogleCalendarEventPreview {
		lookups <- [2]string{calendarID, eventID}
		return &operator_action_servicepb.GoogleCalendarEventPreview{EventId: eventID, Summary: "Facilities coordination"}
	})}
	namespace := newPolicyNamespace(t)
	s := serveWithPolicyOptions(t, newCatalog(t, submissionCatalog), namespace, namespace.watch(t), nil, previews)

	submit := func(name, args string) store.ActionRequest {
		t.Helper()
		id := uuid.New()
		request, err := s.store.Submit(t.Context(), s.caller, store.ActionRequestInput{
			IdempotencyKey: id.String(), Action: store.ActionIdentity{Group: "google_calendar", Name: name},
			Arguments: json.RawMessage(args), Title: name,
		}, store.Admission{RequestID: id})
		if err != nil {
			t.Fatal(err)
		}
		return request
	}
	receipt := submit("delete_event", `{"calendar_id":"facilities@example.test","event_id":"occurrence-1","recurring_event_id":"other-series"}`)
	request := &operator_action_servicepb.GetGoogleCalendarEventPreviewRequest{RequestId: receipt.ID.String()}
	_, err := s.operators.GetGoogleCalendarEventPreview(as(t, callerCertificate), request)
	wantCode(t, "workload cannot read Calendar previews", err, codes.Unauthenticated)

	code, body := s.get(t, "/v1/operator/action-requests/"+receipt.ID.String()+"/google-calendar-event-preview", s.operatorToken)
	var response operator_action_servicepb.GetGoogleCalendarEventPreviewResponse
	if code != http.StatusOK || protojson.Unmarshal(body, &response) != nil || response.GetEvent().GetSummary() != "Facilities coordination" {
		t.Fatalf("Calendar preview response %d %s", code, body)
	}

	instances := submit("list_event_instances", `{"recurring_event_id":"series-2","event_id":"other-event"}`)
	_, err = s.operators.GetGoogleCalendarEventPreview(as(t, s.operatorToken), &operator_action_servicepb.GetGoogleCalendarEventPreviewRequest{RequestId: instances.ID.String()})
	if err != nil {
		t.Fatal(err)
	}
	for _, want := range [][2]string{{"facilities@example.test", "occurrence-1"}, {"primary", "series-2"}} {
		select {
		case got := <-lookups:
			if got != want {
				t.Errorf("looked up %q; want %q", got, want)
			}
		default:
			t.Errorf("no lookup for %q", want)
		}
	}

	for _, invalid := range []store.ActionRequest{
		submit("list_events", `{"calendar_id":"primary","event_id":"other-event"}`),
		submit("update_event", `{"calendar_id":null,"event_id":"other-event"}`),
	} {
		_, err := s.operators.GetGoogleCalendarEventPreview(as(t, s.operatorToken), &operator_action_servicepb.GetGoogleCalendarEventPreviewRequest{RequestId: invalid.ID.String()})
		wantCode(t, "no preview target", err, codes.InvalidArgument)
	}
	if _, err := s.decideAs(t, s.operatorToken, decision(receipt, action_servicepb.Verdict_VERDICT_DENY)); err != nil {
		t.Fatal(err)
	}
	afterDecision, err := s.operators.GetGoogleCalendarEventPreview(as(t, s.operatorToken), request)
	if err != nil || afterDecision.GetEvent() != nil {
		t.Fatalf("preview after decision = %v, %v", afterDecision, err)
	}
	select {
	case got := <-lookups:
		t.Errorf("unexpected lookup %q", got)
	default:
	}
}

type gmailResourceRecorder struct {
	gmailPreviewProvider
	labels     chan []string
	drafts     chan []string
	mailLabels func(string, bool, []string) []*operator_action_servicepb.GmailResourceTitle
}

func (r gmailResourceRecorder) GmailLabelNames(_ context.Context, ids []string) []*operator_action_servicepb.GmailResourceTitle {
	r.labels <- ids
	return nil
}

func (r gmailResourceRecorder) GmailDraftSubjects(_ context.Context, ids []string) []*operator_action_servicepb.GmailResourceTitle {
	r.drafts <- ids
	return nil
}

func (r gmailResourceRecorder) GmailMailLabelNames(_ context.Context, id string, thread bool, labelIDs []string) []*operator_action_servicepb.GmailResourceTitle {
	return r.mailLabels(id, thread, labelIDs)
}

func TestGmailResourceSummariesLookUpOnlyRecordedResources(t *testing.T) {
	gmail := gmailResourceRecorder{labels: make(chan []string, 4), drafts: make(chan []string, 4)}
	namespace := newPolicyNamespace(t)
	s := serveWithPolicyOptions(
		t, newCatalog(t, submissionCatalog), namespace, namespace.watch(t), nil, IntegrationPreviews{Gmail: gmail},
	)
	submit := func(name, args string) store.ActionRequest {
		t.Helper()
		request, err := s.store.Submit(t.Context(), s.caller, store.ActionRequestInput{
			IdempotencyKey: name, Action: store.ActionIdentity{Group: "gmail", Name: name},
			Arguments: json.RawMessage(args), Title: name,
		}, store.Admission{RequestID: uuid.New()})
		if err != nil {
			t.Fatal(err)
		}
		return request
	}
	lookup := func(receipt store.ActionRequest) *operator_action_servicepb.GetGmailResourceSummariesRequest {
		t.Helper()
		request := &operator_action_servicepb.GetGmailResourceSummariesRequest{RequestId: receipt.ID.String()}
		_, err := s.operators.GetGmailResourceSummaries(as(t, s.operatorToken), request)
		if err != nil {
			t.Fatal(err)
		}
		return request
	}

	filter := submit("filters_create", `{"criteria":{"subject":"review"},"action":{"addLabelIds":["Label_19"],"removeLabelIds":["INBOX"]},"label_id":"unrelated-label"}`)
	request := lookup(filter)
	_, err := s.operators.GetGmailResourceSummaries(as(t, callerCertificate), request)
	wantCode(t, "workload cannot read Gmail resource summaries", err, codes.Unauthenticated)
	draft := submit("drafts_delete", `{"draft_id":"test-draft","id":"unrelated-draft"}`)
	lookup(draft)
	s.decide(t, draft, store.VerdictDeny)
	lookup(draft)

	for _, check := range []struct {
		calls chan []string
		want  []string
	}{
		{gmail.labels, []string{"Label_19", "INBOX"}},
		{gmail.drafts, []string{"test-draft"}},
	} {
		select {
		case got := <-check.calls:
			if !slices.Equal(got, check.want) {
				t.Errorf("looked up %q; want %q", got, check.want)
			}
		default:
			t.Errorf("did not look up %q", check.want)
		}
		select {
		case got := <-check.calls:
			t.Errorf("unexpected lookup %q", got)
		default:
		}
	}
}

func TestGmailMailLabelsUseOnlyTheMatchingSuccessfulReceipt(t *testing.T) {
	var labelCalls atomic.Int32
	gmail := gmailResourceRecorder{mailLabels: func(id string, thread bool, ids []string) []*operator_action_servicepb.GmailResourceTitle {
		labelCalls.Add(1)
		expectedID, expectedLabels := "message-1", []string{"Label_19"}
		if thread {
			expectedID, expectedLabels = "thread-1", []string{"Label_19", "Label_23"}
		}
		if id != expectedID || !slices.Equal(ids, expectedLabels) {
			t.Errorf("looked up %q on %q, thread=%v", ids, id, thread)
		}
		return []*operator_action_servicepb.GmailResourceTitle{{Id: "Label_19", Title: "Deployments/Reviewed"}}
	}}
	namespace := newPolicyNamespace(t)
	s := serveWithPolicyOptions(
		t, newCatalog(t, submissionCatalog), namespace, namespace.watch(t), nil, IntegrationPreviews{Gmail: gmail},
	)
	for _, tc := range []struct {
		name, args, rawResult string
		wantLabels            bool
	}{
		{
			"messages_get", `{"id":"message-1","labelIds":["Label_43"]}`,
			`{"content":[],"structuredContent":{"id":"message-1","labelIds":["INBOX","Label_19","Label_19"]}}`, true,
		},
		{
			"threads_get", `{"id":"thread-1"}`,
			`{"content":[{"type":"text","text":"{\"id\":\"thread-1\",\"messages\":[{\"labelIds\":[\"Label_19\",\"../invalid\"]},{\"labelIds\":[\"Label_23\",\"SENT\"]}]}"}]}`, true,
		},
		{
			"threads_get", `{"id":"thread-2"}`,
			`{"content":[],"structuredContent":{"id":"other-thread","messages":[{"labelIds":["Label_82"]}]}}`, false,
		},
		{
			"threads_get", `{"id":"thread-3"}`,
			`{"content":[],"isError":true,"structuredContent":{"id":"thread-3","messages":[{"labelIds":["Label_82"]}]}}`, false,
		},
	} {
		request, err := s.store.Submit(t.Context(), s.caller, store.ActionRequestInput{
			IdempotencyKey: tc.args, Action: store.ActionIdentity{Group: "gmail", Name: tc.name},
			Arguments: json.RawMessage(tc.args), Title: tc.name,
		}, store.Admission{RequestID: uuid.New()})
		if err != nil {
			t.Fatal(err)
		}
		s.decide(t, request, store.VerdictAllow)
		claim, err := s.store.ClaimExecution(t.Context(), request.ID, "test-executor", time.Minute, func(store.ActionIdentity) bool { return true })
		if err != nil || claim == nil {
			t.Fatalf("claim: %v, %v", claim, err)
		}
		if _, err := s.store.MarkRunning(t.Context(), request.ID); err != nil {
			t.Fatal(err)
		}
		if err := s.store.FinishExecution(t.Context(), request.ID, claim.Lease, store.ExecutionOutcome{
			State: store.ExecutionSucceeded, Result: json.RawMessage(tc.rawResult),
		}); err != nil {
			t.Fatal(err)
		}
		response, err := s.operators.GetGmailResourceSummaries(as(t, s.operatorToken), &operator_action_servicepb.GetGmailResourceSummariesRequest{RequestId: request.ID.String()})
		if err != nil || (len(response.GetLabels()) != 0) != tc.wantLabels {
			t.Fatalf("receipt %s: %v, %v", tc.args, response, err)
		}
	}
	if count := labelCalls.Load(); count != 2 {
		t.Errorf("provider received %d label calls, want 2", count)
	}
}
