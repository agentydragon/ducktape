package store

import (
	"testing"

	dispatcherpb "github.com/agentydragon/ducktape/agentplane/session_dispatcher/dispatcher/api/session/session_dispatcherpb"
)

func TestDescribeDispatchMapsUnknownStoredStateToUnknown(t *testing.T) {
	dispatch, err := DescribeDispatch(&Dispatch{State: "FUTURE_STATE", Operation: "submit_input"}, false)
	if err != nil {
		t.Fatalf("DescribeDispatch() error = %v", err)
	}
	if got := dispatch.GetState(); got != dispatcherpb.DeliveryState_DELIVERY_STATE_UNKNOWN {
		t.Fatalf("Dispatch state = %s, want unknown", got)
	}
}
