package store

import (
	"fmt"

	commandpb "github.com/agentydragon/ducktape/agentplane/proto/command"
	"github.com/agentydragon/ducktape/agentplane/session_dispatcher/dispatcher/api/session/dispatchstate"
	dispatcherpb "github.com/agentydragon/ducktape/agentplane/session_dispatcher/dispatcher/api/session/session_dispatcherpb"
	"google.golang.org/protobuf/types/known/timestamppb"
)

func validateDispatchOperation(op commandpb.Operation) error {
	switch op {
	case commandpb.Operation_OPERATION_SUBMIT_INPUT, commandpb.Operation_OPERATION_CHANGE_MODEL,
		commandpb.Operation_OPERATION_CHANGE_REASONING_EFFORT, commandpb.Operation_OPERATION_INTERRUPT_TURN:
		return nil
	default:
		return fmt.Errorf("unknown dispatch operation %d", op)
	}
}

// DescribeDispatch is the single wire projection for reads and terminal Watch
// events. It never substitutes runner admission for a queued receipt.
func DescribeDispatch(row *Dispatch, includeCommand bool) (*dispatcherpb.Dispatch, error) {
	if err := validateDispatchOperation(row.Operation); err != nil {
		return nil, err
	}
	result := &dispatcherpb.Dispatch{
		CommandId: row.CommandID, SessionId: row.SessionID, Sandbox: row.SandboxName,
		SandboxUid: row.SandboxUID, Operation: row.Operation, State: dispatchstate.NormalizeDeliveryState(row.State),
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
