package actions

import (
	"strings"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/action_service/api/contracts/action_servicepb"
	"github.com/agentydragon/ducktape/agentplane/proto/identity"
	"google.golang.org/protobuf/types/known/structpb"
	"google.golang.org/protobuf/types/known/timestamppb"
)

func TestActionText(t *testing.T) {
	group, err := groupLine(&action_servicepb.ActionGroup{
		Key: "test-group", Title: "Test", ExecutorKind: action_servicepb.ExecutorKind_EXECUTOR_KIND_MCP,
		Available: true, Actions: []*action_servicepb.Action{{}, {}},
	})
	if err != nil || group != "test-group  mcp  2 actions  Test" {
		t.Fatalf("group text = %q, error %v", group, err)
	}

	action, err := actionDetail(&action_servicepb.Action{
		Group: "test-group", Name: "echo", Description: "Echo text",
		InputSchema: &structpb.Struct{Fields: map[string]*structpb.Value{"type": structpb.NewStringValue("object")}},
	})
	if err != nil || !strings.Contains(action, "input schema:\n{") || !strings.Contains(action, `"object"`) {
		t.Fatalf("action detail = %q, error %v", action, err)
	}

	policy, err := policyText(&action_servicepb.CallerActionPolicy{
		Subject: &identity.ServiceAccount{Namespace: "test", Name: "agent"}, Synced: true,
		Bindings: []*action_servicepb.CallerBinding{{
			Name: "binding", ExpiresAt: timestamppb.New(time.Date(2026, 1, 2, 3, 4, 5, 0, time.UTC)),
			PolicySets: []*action_servicepb.ActionPolicySet{{Name: "set", Generation: 1}},
		}},
		AutoApproveIf: []*action_servicepb.EffectivePolicy{{
			PolicySet: "set", Policy: &action_servicepb.Policy{Kind: &action_servicepb.Policy_ExactActions{
				ExactActions: &action_servicepb.ExactActionsPolicy{Actions: []*action_servicepb.GroupActions{{Group: "test-group", Names: []string{"echo"}}}},
			}},
		}},
	})
	wantPolicy := "subject test/agent  synced true\nbinding binding  sets set  expires 2026-01-02T03:04:05Z\nauto-approve set#0  exact_actions  test-group/echo"
	if err != nil || policy != wantPolicy {
		t.Fatalf("policy text = %q, error %v; want %q", policy, err, wantPolicy)
	}

	cancellation, err := cancellationLine(&action_servicepb.CancelActionRequestResponse{
		Outcome: action_servicepb.CancellationOutcome_CANCELLATION_OUTCOME_CANCELLED,
		ActionRequest: &action_servicepb.ActionRequest{
			Id: "request-id", Action: &action_servicepb.ActionIdentity{Group: "test-group", Name: "echo"},
			State: action_servicepb.ActionState_ACTION_STATE_CANCELLED, Title: "Stop it",
		},
	})
	if err != nil || cancellation != `cancelled  request-id  cancelled  test-group/echo  "Stop it"` {
		t.Fatalf("cancellation text = %q, error %v", cancellation, err)
	}
}
