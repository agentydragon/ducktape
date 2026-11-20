package rpc

import (
	"context"
	"encoding/json"
	"net/http"
	"slices"
	"testing"

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

type tanaSummariesFunc func(context.Context, []string) []*operator_action_servicepb.TanaNodeSummary

func (f tanaSummariesFunc) TanaNodeSummaries(ctx context.Context, ids []string) []*operator_action_servicepb.TanaNodeSummary {
	return f(ctx, ids)
}

func TestIntegrationSummariesUseOnlyRecordedIDs(t *testing.T) {
	gmailIDs := make(chan []string, 1)
	tanaIDs := make(chan []string, 1)
	previews := IntegrationPreviews{
		Gmail: gmailSummariesFunc(func(_ context.Context, ids []string) []*operator_action_servicepb.GmailThreadSummary {
			select {
			case gmailIDs <- ids:
			default:
				t.Error("unexpected extra Gmail lookup")
			}
			return []*operator_action_servicepb.GmailThreadSummary{{ThreadId: "thread-1", Subject: "Deployment review"}}
		}),
		Tana: tanaSummariesFunc(func(_ context.Context, ids []string) []*operator_action_servicepb.TanaNodeSummary {
			select {
			case tanaIDs <- ids:
			default:
				t.Error("unexpected extra Tana lookup")
			}
			return []*operator_action_servicepb.TanaNodeSummary{{NodeId: "target", Name: "Destination"}}
		}),
	}
	namespace := newPolicyNamespace(t)
	s := serveWithPolicyOptions(t, newCatalog(t, submissionCatalog), namespace, namespace.watch(t), nil, previews)
	submit := func(group, name, args string) store.ActionRequest {
		request, err := s.store.Submit(t.Context(), s.caller, store.ActionRequestInput{
			IdempotencyKey: group, Action: store.ActionIdentity{Group: group, Name: name},
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

	for _, tc := range []struct {
		ids     chan []string
		want    []string
		receipt store.ActionRequest
	}{
		{gmailIDs, []string{"thread-1", "2", "3", "4", "5", "6", "7", "8"}, gmail},
		{tanaIDs, []string{"draft", "target", "source"}, tana},
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
