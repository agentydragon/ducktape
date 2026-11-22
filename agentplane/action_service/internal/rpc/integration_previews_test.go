package rpc

import (
	"context"
	"encoding/json"
	"net/http"
	"slices"
	"sync"
	"sync/atomic"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/action_service/api/contracts/action_servicepb"
	"github.com/agentydragon/ducktape/agentplane/action_service/api/contracts/operator_action_servicepb"
	"github.com/agentydragon/ducktape/agentplane/action_service/internal/store"
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
	gmail := s.submitAction(t, "gmail", "threads_modify_labels", `{"thread_ids":["thread-1","thread-1","../other","2","3","4","5","6","7","8","9"]}`)
	tana := s.submitAction(t, "tana", "move_node", `{"nodeId":"draft","targetNodeId":"target","sourceParentId":"source","referenceNodeId":"draft"}`)
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

	calendar := s.submitAction(t, "tana", "get_or_create_calendar_node", `{"workspaceId":"workspace-123","nodeId":"unrelated"}`)
	calendarResponse, err := s.operators.GetTanaNodeSummaries(as(t, s.operatorToken), &operator_action_servicepb.GetTanaNodeSummariesRequest{RequestId: calendar.ID.String()})
	if err != nil || calendarResponse.GetWorkspace().GetName() != "Product" || len(calendarResponse.GetNodes()) != 0 {
		t.Fatalf("workspace lookup: %v, %v", calendarResponse, err)
	}

	invalid := s.submitAction(t, "tana", "list_workspaces", `{"workspaceId":"../other"}`)
	response, err := s.operators.GetTanaNodeSummaries(as(t, s.operatorToken), &operator_action_servicepb.GetTanaNodeSummariesRequest{RequestId: invalid.ID.String()})
	wantCode(t, "unsupported Tana action", err, codes.InvalidArgument)
	if response != nil {
		t.Fatalf("unsupported action returned %v", response)
	}

	checkbox := s.submitAction(t, "tana", "set_tag_checkbox", `{
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
	lookup := func(request store.ActionRequest) {
		t.Helper()
		_, err := s.operators.GetGmailThreadSummaries(as(t, s.operatorToken), &operator_action_servicepb.GetGmailThreadSummariesRequest{RequestId: request.ID.String()})
		if err != nil {
			t.Fatal(err)
		}
	}
	lookup(s.submitAction(t, "gmail", "drafts_create", `{"thread_id":"reply-thread","thread_ids":["unrelated-thread"]}`))
	search := s.submitAction(t, "gmail", "threads_list", `{"thread_id":"unrelated-thread","q":"test"}`)
	lookup(search)
	s.decide(t, search, store.VerdictAllow)
	s.execute(t, search, store.ExecutionOutcome{
		State:  store.ExecutionSucceeded,
		Result: json.RawMessage(`{"content":[],"structuredContent":{"threads":[{"id":"returned-thread"},{"id":"returned-thread"},{"id":"../invalid"}]}}`),
	})
	lookup(search)
	mu.Lock()
	defer mu.Unlock()
	if want := []string{"reply-thread", "returned-thread"}; !slices.Equal(lookedUp, want) {
		t.Errorf("looked up %q; want %q", lookedUp, want)
	}
}

type calendarPreviewFuncs struct {
	event   func(context.Context, string, string) *operator_action_servicepb.GoogleCalendarEventPreview
	summary func(context.Context, string) *operator_action_servicepb.GoogleCalendarSummary
}

func (f calendarPreviewFuncs) GoogleCalendarEventPreview(ctx context.Context, calendarID, eventID string) *operator_action_servicepb.GoogleCalendarEventPreview {
	return f.event(ctx, calendarID, eventID)
}

func (f calendarPreviewFuncs) GoogleCalendarSummary(ctx context.Context, calendarID string) *operator_action_servicepb.GoogleCalendarSummary {
	return f.summary(ctx, calendarID)
}

func TestGoogleCalendarEventPreviewUsesOnlyPendingRecordedTargets(t *testing.T) {
	eventLookups := make(chan [2]string, 4)
	calendarLookups := make(chan string, 4)
	previews := IntegrationPreviews{GoogleCalendar: calendarPreviewFuncs{
		event: func(_ context.Context, calendarID, eventID string) *operator_action_servicepb.GoogleCalendarEventPreview {
			eventLookups <- [2]string{calendarID, eventID}
			return &operator_action_servicepb.GoogleCalendarEventPreview{EventId: eventID, Summary: "Facilities coordination"}
		},
		summary: func(_ context.Context, calendarID string) *operator_action_servicepb.GoogleCalendarSummary {
			calendarLookups <- calendarID
			return &operator_action_servicepb.GoogleCalendarSummary{CalendarId: calendarID, Summary: "Facilities calendar"}
		},
	}}
	namespace := newPolicyNamespace(t)
	s := serveWithPolicyOptions(t, newCatalog(t, submissionCatalog), namespace, namespace.watch(t), nil, previews)

	receipt := s.submitAction(t, "google_calendar", "delete_event", `{"calendar_id":"facilities@example.test","event_id":"occurrence-1","recurring_event_id":"other-series"}`)
	request := &operator_action_servicepb.GetGoogleCalendarEventPreviewRequest{RequestId: receipt.ID.String()}
	_, err := s.operators.GetGoogleCalendarEventPreview(as(t, callerCertificate), request)
	wantCode(t, "workload cannot read Calendar previews", err, codes.Unauthenticated)

	code, body := s.get(t, "/v1/operator/action-requests/"+receipt.ID.String()+"/google-calendar-event-preview", s.operatorToken)
	var response operator_action_servicepb.GetGoogleCalendarEventPreviewResponse
	if code != http.StatusOK || protojson.Unmarshal(body, &response) != nil ||
		response.GetEvent().GetSummary() != "Facilities coordination" ||
		response.GetCalendar().GetSummary() != "Facilities calendar" {
		t.Fatalf("Calendar preview response %d %s", code, body)
	}

	instances := s.submitAction(t, "google_calendar", "list_event_instances", `{"recurring_event_id":"series-2","event_id":"other-event"}`)
	_, err = s.operators.GetGoogleCalendarEventPreview(as(t, s.operatorToken), &operator_action_servicepb.GetGoogleCalendarEventPreviewRequest{RequestId: instances.ID.String()})
	if err != nil {
		t.Fatal(err)
	}
	for _, want := range [][2]string{{"facilities@example.test", "occurrence-1"}, {"primary", "series-2"}} {
		select {
		case got := <-eventLookups:
			if got != want {
				t.Errorf("looked up %q; want %q", got, want)
			}
		default:
			t.Errorf("no lookup for %q", want)
		}
	}

	for _, tc := range []struct{ action, args, calendarID string }{
		{"create_event", `{"calendar_id":"team@example.test","event_id":"unrelated"}`, "team@example.test"},
		{"list_events", `{"calendar_id":"shifts@example.test","event_id":"unrelated"}`, "shifts@example.test"},
		{"list_events", `{"calendar_id":"primary","event_id":"unrelated"}`, ""},
	} {
		recorded := s.submitAction(t, "google_calendar", tc.action, tc.args)
		got, err := s.operators.GetGoogleCalendarEventPreview(as(t, s.operatorToken), &operator_action_servicepb.GetGoogleCalendarEventPreviewRequest{RequestId: recorded.ID.String()})
		if err != nil || got.GetEvent() != nil || got.GetCalendar().GetCalendarId() != tc.calendarID {
			t.Fatalf("%s preview = %v, %v", tc.action, got, err)
		}
	}
	for _, want := range []string{"facilities@example.test", "team@example.test", "shifts@example.test"} {
		select {
		case got := <-calendarLookups:
			if got != want {
				t.Errorf("looked up calendar %q; want %q", got, want)
			}
		default:
			t.Errorf("no calendar lookup for %q", want)
		}
	}

	for _, invalid := range []store.ActionRequest{
		s.submitAction(t, "google_calendar", "unknown", `{"calendar_id":"unrelated@example.test","event_id":"other-event"}`),
		s.submitAction(t, "google_calendar", "update_event", `{"calendar_id":null,"event_id":"other-event"}`),
	} {
		_, err := s.operators.GetGoogleCalendarEventPreview(as(t, s.operatorToken), &operator_action_servicepb.GetGoogleCalendarEventPreviewRequest{RequestId: invalid.ID.String()})
		wantCode(t, "no preview target", err, codes.InvalidArgument)
	}
	if _, err := s.decideAs(t, s.operatorToken, decision(receipt, action_servicepb.Verdict_VERDICT_DENY)); err != nil {
		t.Fatal(err)
	}
	afterDecision, err := s.operators.GetGoogleCalendarEventPreview(as(t, s.operatorToken), request)
	if err != nil || afterDecision.GetEvent() != nil || afterDecision.GetCalendar() != nil {
		t.Fatalf("preview after decision = %v, %v", afterDecision, err)
	}
	select {
	case got := <-eventLookups:
		t.Errorf("unexpected event lookup %q", got)
	case got := <-calendarLookups:
		t.Errorf("unexpected calendar lookup %q", got)
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
	lookup := func(receipt store.ActionRequest) *operator_action_servicepb.GetGmailResourceSummariesRequest {
		t.Helper()
		request := &operator_action_servicepb.GetGmailResourceSummariesRequest{RequestId: receipt.ID.String()}
		_, err := s.operators.GetGmailResourceSummaries(as(t, s.operatorToken), request)
		if err != nil {
			t.Fatal(err)
		}
		return request
	}

	filter := s.submitAction(t, "gmail", "filters_create", `{"criteria":{"subject":"review"},"action":{"addLabelIds":["Label_19"],"removeLabelIds":["INBOX"]},"label_id":"unrelated-label"}`)
	request := lookup(filter)
	_, err := s.operators.GetGmailResourceSummaries(as(t, callerCertificate), request)
	wantCode(t, "workload cannot read Gmail resource summaries", err, codes.Unauthenticated)
	draft := s.submitAction(t, "gmail", "drafts_delete", `{"draft_id":"test-draft","id":"unrelated-draft"}`)
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
		request := s.submitAction(t, "gmail", tc.name, tc.args)
		s.decide(t, request, store.VerdictAllow)
		s.execute(t, request, store.ExecutionOutcome{State: store.ExecutionSucceeded, Result: json.RawMessage(tc.rawResult)})
		response, err := s.operators.GetGmailResourceSummaries(as(t, s.operatorToken), &operator_action_servicepb.GetGmailResourceSummariesRequest{RequestId: request.ID.String()})
		if err != nil || (len(response.GetLabels()) != 0) != tc.wantLabels {
			t.Fatalf("receipt %s: %v, %v", tc.args, response, err)
		}
	}
	if count := labelCalls.Load(); count != 2 {
		t.Errorf("provider received %d label calls, want 2", count)
	}
}

type githubLabelRecorder struct{ calls chan []string }

func (r githubLabelRecorder) GitHubLabelSummaries(_ context.Context, owner, repo string, names []string) []*operator_action_servicepb.GitHubLabelSummary {
	r.calls <- append([]string{owner, repo}, names...)
	return []*operator_action_servicepb.GitHubLabelSummary{{Name: names[0], Color: "d73a4a"}}
}

func TestGitHubLabelSummariesUseOnlyRecordedIssueLabels(t *testing.T) {
	recorder := githubLabelRecorder{calls: make(chan []string, 1)}
	namespace := newPolicyNamespace(t)
	s := serveWithPolicyOptions(t, newCatalog(t, submissionCatalog), namespace, namespace.watch(t), nil, IntegrationPreviews{GitHub: recorder})
	receipt := s.submitAction(t, "github", "issue_write", `{"method":"update","owner":"owner","repo":"repo","issue_number":42,"labels":["bug","needs review","bug","3","4","5","6","7","8","9"]}`)
	req := &operator_action_servicepb.GetGitHubLabelSummariesRequest{RequestId: receipt.ID.String()}
	_, err := s.operators.GetGitHubLabelSummaries(as(t, callerCertificate), req)
	wantCode(t, "workload cannot read GitHub label summaries", err, codes.Unauthenticated)
	code, body := s.get(t, "/v1/operator/action-requests/"+receipt.ID.String()+"/github-label-summaries", s.operatorToken)
	var response operator_action_servicepb.GetGitHubLabelSummariesResponse
	if code != http.StatusOK || protojson.Unmarshal(body, &response) != nil || len(response.Labels) != 1 || response.Labels[0].GetColor() != "d73a4a" {
		t.Fatalf("GitHub summary HTTP response %d %s", code, body)
	}
	if got := <-recorder.calls; !slices.Equal(got, []string{"owner", "repo", "bug", "needs review", "3", "4", "5", "6", "7", "8"}) {
		t.Errorf("looked up %q", got)
	}
	wrong := s.submitAction(t, "gmail", "labels_get", `{"id":"bug"}`)
	_, err = s.operators.GetGitHubLabelSummaries(as(t, s.operatorToken), &operator_action_servicepb.GetGitHubLabelSummariesRequest{RequestId: wrong.ID.String()})
	wantCode(t, "wrong integration", err, codes.InvalidArgument)
}
