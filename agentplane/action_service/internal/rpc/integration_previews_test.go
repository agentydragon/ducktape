package rpc

import (
	"context"
	"encoding/json"
	"net/http"
	"slices"
	"sync"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/action_service/api/contracts/operator_action_servicepb"
	"github.com/agentydragon/ducktape/agentplane/action_service/internal/store"
	"github.com/google/uuid"
	"google.golang.org/grpc/codes"
	"google.golang.org/protobuf/encoding/protojson"
)

type gmailSummariesFunc func(context.Context, []string) []*operator_action_servicepb.GmailThreadSummary

func (f gmailSummariesFunc) GmailThreadSummaries(ctx context.Context, ids []string) []*operator_action_servicepb.GmailThreadSummary {
	return f(ctx, ids)
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
		Gmail: gmailSummariesFunc(func(_ context.Context, ids []string) []*operator_action_servicepb.GmailThreadSummary {
			select {
			case gmailIDs <- ids:
			default:
				t.Error("unexpected extra Gmail lookup")
			}
			return []*operator_action_servicepb.GmailThreadSummary{{ThreadId: "thread-1", Subject: "Deployment review"}}
		}),
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
		Gmail: gmailSummariesFunc(func(_ context.Context, ids []string) []*operator_action_servicepb.GmailThreadSummary {
			mu.Lock()
			defer mu.Unlock()
			lookedUp = append(lookedUp, ids...)
			return nil
		}),
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
