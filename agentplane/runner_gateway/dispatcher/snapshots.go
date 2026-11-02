package dispatcher

import (
	"fmt"

	dispatcherpb "github.com/agentydragon/ducktape/agentplane/runner_gateway/dispatcher/session_dispatcherpb"
	"google.golang.org/protobuf/types/known/timestamppb"
)

func dispatchState(state string) (dispatcherpb.DeliveryState, error) {
	switch state {
	case QueueState:
		return dispatcherpb.DeliveryState_DELIVERY_STATE_QUEUED_IN_DISPATCHER, nil
	case DeliveringState:
		return dispatcherpb.DeliveryState_DELIVERY_STATE_DELIVERING_TO_RUNNER, nil
	case AdmittedState:
		return dispatcherpb.DeliveryState_DELIVERY_STATE_RUNNER_ADMITTED, nil
	case "HARNESS_CONFIRMED":
		return dispatcherpb.DeliveryState_DELIVERY_STATE_HARNESS_CONFIRMED, nil
	case "NO_OP":
		return dispatcherpb.DeliveryState_DELIVERY_STATE_NO_OP, nil
	case FailedState:
		return dispatcherpb.DeliveryState_DELIVERY_STATE_FAILED, nil
	case CancelledState:
		return dispatcherpb.DeliveryState_DELIVERY_STATE_CANCELLED_IN_DISPATCHER, nil
	case UnknownState:
		return dispatcherpb.DeliveryState_DELIVERY_STATE_UNKNOWN, nil
	default:
		return 0, fmt.Errorf("unknown dispatch state %q", state)
	}
}

func dispatchOperation(operation string) (dispatcherpb.Operation, error) {
	switch operation {
	case "submit_input":
		return dispatcherpb.Operation_OPERATION_SUBMIT_INPUT, nil
	case "change_model":
		return dispatcherpb.Operation_OPERATION_CHANGE_MODEL, nil
	case "change_reasoning_effort":
		return dispatcherpb.Operation_OPERATION_CHANGE_REASONING_EFFORT, nil
	default:
		return 0, fmt.Errorf("unknown dispatch operation %q", operation)
	}
}

// DescribeDispatch is the single wire projection for reads and terminal Watch
// events. It never substitutes runner admission for a queued receipt.
func DescribeDispatch(row *Dispatch) (*dispatcherpb.Dispatch, error) {
	state, err := dispatchState(row.State)
	if err != nil {
		return nil, err
	}
	operation, err := dispatchOperation(row.Operation)
	if err != nil {
		return nil, err
	}
	result := &dispatcherpb.Dispatch{
		CommandId: row.CommandID, SessionId: row.SessionID, Sandbox: row.SandboxName,
		SandboxUid: row.SandboxUID, Operation: operation, State: state,
		Admission: row.Admission, CreatedAt: timestamppb.New(row.CreatedAt), UpdatedAt: timestamppb.New(row.UpdatedAt),
		DeliveryBlocker: row.DeliveryBlocker, WakeAssessment: row.WakeAssessment,
	}
	if row.Reason != "" {
		result.Reason = &row.Reason
	}
	return result, nil
}

func DescribeOpening(row *Opening) (*dispatcherpb.SessionOpening, error) {
	state, err := dispatchState(row.State)
	if err != nil {
		return nil, err
	}
	result := &dispatcherpb.SessionOpening{
		SessionId: row.SessionID, Sandbox: row.SandboxName, SandboxUid: row.SandboxUID,
		State: state, CreatedAt: timestamppb.New(row.CreatedAt), UpdatedAt: timestamppb.New(row.UpdatedAt),
		DeliveryBlocker: row.DeliveryBlocker, WakeAssessment: row.WakeAssessment,
	}
	if row.Reason != "" {
		result.Reason = &row.Reason
	}
	return result, nil
}
