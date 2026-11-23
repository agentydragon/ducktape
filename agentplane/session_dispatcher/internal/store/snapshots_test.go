package store

import (
	"testing"

	commandpb "github.com/agentydragon/ducktape/agentplane/proto/command"
	dispatcherpb "github.com/agentydragon/ducktape/agentplane/session_dispatcher/dispatcher/api/session/session_dispatcherpb"
)

func TestDescribeDispatchMapsUnknownStoredStateToUnknown(t *testing.T) {
	dispatch, err := DescribeDispatch(&Dispatch{State: dispatcherpb.DeliveryState(99), Operation: commandpb.Operation_OPERATION_SUBMIT_INPUT}, false)
	if err != nil {
		t.Fatalf("DescribeDispatch() error = %v", err)
	}
	if got := dispatch.GetState(); got != dispatcherpb.DeliveryState_DELIVERY_STATE_UNKNOWN {
		t.Fatalf("Dispatch state = %s, want unknown", got)
	}
}
