package sessiondelegation

import (
	"encoding/json"
	"testing"

	store "github.com/agentydragon/ducktape/agentplane/action_service/store"
	environmentpb "github.com/agentydragon/ducktape/agentplane/proto/environmentlifecyclepb"
	dispatcherpb "github.com/agentydragon/ducktape/agentplane/runner_gateway/dispatcher/session_dispatcherpb"
	"google.golang.org/protobuf/types/known/timestamppb"
)

func TestObserveActionPreservesQueuedWakeAssessment(t *testing.T) {
	backend := &delegatedBackend{testing: t, lookup: &dispatcherpb.Dispatch{
		CommandId: testCommandID, SessionId: testSessionID, Operation: dispatcherpb.Operation_OPERATION_SUBMIT_INPUT,
		State: dispatcherpb.DeliveryState_DELIVERY_STATE_QUEUED_IN_DISPATCHER,
		DeliveryBlocker: &environmentpb.DeliveryBlocker{
			Kind: environmentpb.DeliveryBlockerKind_DELIVERY_BLOCKER_KIND_ENVIRONMENT_SUSPENDED,
		},
		WakeAssessment: &environmentpb.WakeAssessment{
			State:      environmentpb.WakeState_WAKE_STATE_DENIED,
			Reason:     environmentpb.WakeReason_WAKE_REASON_CALLER_NOT_AUTHORIZED,
			ObservedAt: timestamppb.Now(),
		},
	}}
	executor := newDelegatedExecutor(t, backend)
	uid := testCallerUID
	outcome, err := executor.attempt(t.Context(), requestFor("observe_input", map[string]any{
		"session_id": testSessionID, "command_id": testCommandID, "wait_seconds": 0,
	}, &uid))
	if err != nil || outcome.State != store.ExecutionSucceeded {
		t.Fatalf("outcome=%+v error=%v", outcome, err)
	}
	var result struct {
		Dispatch struct {
			Value struct {
				State           string `json:"state"`
				DeliveryBlocker struct {
					Kind string `json:"kind"`
				} `json:"deliveryBlocker"`
				WakeAssessment struct {
					State  string `json:"state"`
					Reason string `json:"reason"`
				} `json:"wakeAssessment"`
			} `json:"value"`
		} `json:"dispatch"`
	}
	if err := json.Unmarshal(outcome.Result, &result); err != nil {
		t.Fatal(err)
	}
	if result.Dispatch.Value.State != dispatcherpb.DeliveryState_DELIVERY_STATE_QUEUED_IN_DISPATCHER.String() ||
		result.Dispatch.Value.DeliveryBlocker.Kind != environmentpb.DeliveryBlockerKind_DELIVERY_BLOCKER_KIND_ENVIRONMENT_SUSPENDED.String() ||
		result.Dispatch.Value.WakeAssessment.State != environmentpb.WakeState_WAKE_STATE_DENIED.String() ||
		result.Dispatch.Value.WakeAssessment.Reason != environmentpb.WakeReason_WAKE_REASON_CALLER_NOT_AUTHORIZED.String() {
		t.Fatalf("Action observe result lost queued wake facts: %+v", result.Dispatch.Value)
	}
}
