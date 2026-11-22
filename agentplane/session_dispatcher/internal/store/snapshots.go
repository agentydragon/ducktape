package store

import (
	"fmt"

	commandpb "github.com/agentydragon/ducktape/agentplane/proto/command"
	dispatcherpb "github.com/agentydragon/ducktape/agentplane/session_dispatcher/dispatcher/api/session/session_dispatcherpb"
	"google.golang.org/protobuf/types/known/timestamppb"
)

func dispatchState(state string) dispatcherpb.DeliveryState {
	switch state {
	case QueueState:
		return dispatcherpb.DeliveryState_DELIVERY_STATE_QUEUED_IN_DISPATCHER
	case DeliveringState:
		return dispatcherpb.DeliveryState_DELIVERY_STATE_DELIVERING_TO_RUNNER
	case AdmittedState:
		return dispatcherpb.DeliveryState_DELIVERY_STATE_RUNNER_ADMITTED
	case FailedState:
		return dispatcherpb.DeliveryState_DELIVERY_STATE_FAILED
	case CancelledState:
		return dispatcherpb.DeliveryState_DELIVERY_STATE_CANCELLED_IN_DISPATCHER
	default:
		return dispatcherpb.DeliveryState_DELIVERY_STATE_UNKNOWN
	}
}

func dispatchOperation(operation string) (commandpb.Operation, error) {
	switch operation {
	case "submit_input":
		return commandpb.Operation_OPERATION_SUBMIT_INPUT, nil
	case "change_model":
		return commandpb.Operation_OPERATION_CHANGE_MODEL, nil
	case "change_reasoning_effort":
		return commandpb.Operation_OPERATION_CHANGE_REASONING_EFFORT, nil
	case "interrupt_turn":
		return commandpb.Operation_OPERATION_INTERRUPT_TURN, nil
	default:
		return 0, fmt.Errorf("unknown dispatch operation %q", operation)
	}
}

// DescribeDispatch is the single wire projection for reads and terminal Watch
// events. It never substitutes runner admission for a queued receipt.
func DescribeDispatch(row *Dispatch, includeCommand bool) (*dispatcherpb.Dispatch, error) {
	operation, err := dispatchOperation(row.Operation)
	if err != nil {
		return nil, err
	}
	result := &dispatcherpb.Dispatch{
		CommandId: row.CommandID, SessionId: row.SessionID, Sandbox: row.SandboxName,
		SandboxUid: row.SandboxUID, Operation: operation, State: dispatchState(row.State),
		Admission: row.Admission, CreatedAt: timestamppb.New(row.CreatedAt), UpdatedAt: timestamppb.New(row.UpdatedAt),
		DeliveryBlocker: row.DeliveryBlocker, WakeAssessment: row.WakeAssessment,
	}
	if includeCommand {
		result.Command = row.Command
	}
	if row.Reason != "" {
		result.Reason = &row.Reason
	}
	return result, nil
}
