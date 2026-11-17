package store

import (
	"fmt"

	commandpb "github.com/agentydragon/ducktape/agentplane/proto/command"
	"github.com/agentydragon/ducktape/agentplane/session_dispatcher/dispatcher/api/session/dispatchstate"
	dispatcherpb "github.com/agentydragon/ducktape/agentplane/session_dispatcher/dispatcher/api/session/session_dispatcherpb"
	"google.golang.org/protobuf/types/known/timestamppb"
)

// dispatchState maps stored state names onto DeliveryState; the stored names are the
// proto names without their DELIVERY_STATE_ prefix. Unknown or unspecified names
// normalize to the named unknown state.
func dispatchState(state string) dispatcherpb.DeliveryState {
	return dispatchstate.NormalizeDeliveryState(dispatcherpb.DeliveryState(dispatcherpb.DeliveryState_value["DELIVERY_STATE_"+state]))
}

var dispatchOperations = map[string]commandpb.Operation{
	"submit_input":            commandpb.Operation_OPERATION_SUBMIT_INPUT,
	"change_model":            commandpb.Operation_OPERATION_CHANGE_MODEL,
	"change_reasoning_effort": commandpb.Operation_OPERATION_CHANGE_REASONING_EFFORT,
	"interrupt_turn":          commandpb.Operation_OPERATION_INTERRUPT_TURN,
}

func dispatchOperation(operation string) (commandpb.Operation, error) {
	op, ok := dispatchOperations[operation]
	if !ok {
		return 0, fmt.Errorf("unknown dispatch operation %q", operation)
	}
	return op, nil
}

// DescribeDispatch is the single wire projection for reads and terminal Watch
// events. It never substitutes runner admission for a queued receipt.
func DescribeDispatch(row *Dispatch) (*dispatcherpb.Dispatch, error) {
	state := dispatchState(row.State)
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
