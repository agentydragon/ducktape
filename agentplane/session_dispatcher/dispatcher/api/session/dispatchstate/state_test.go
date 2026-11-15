package dispatchstate

import (
	"testing"

	dispatcherpb "github.com/agentydragon/ducktape/agentplane/session_dispatcher/dispatcher/api/session/session_dispatcherpb"
)

func TestNormalizeDeliveryState(t *testing.T) {
	tests := []struct {
		name  string
		state dispatcherpb.DeliveryState
		want  dispatcherpb.DeliveryState
	}{
		{"unspecified", dispatcherpb.DeliveryState_DELIVERY_STATE_UNSPECIFIED, dispatcherpb.DeliveryState_DELIVERY_STATE_UNKNOWN},
		{"queued", dispatcherpb.DeliveryState_DELIVERY_STATE_QUEUED_IN_DISPATCHER, dispatcherpb.DeliveryState_DELIVERY_STATE_QUEUED_IN_DISPATCHER},
		{"delivering", dispatcherpb.DeliveryState_DELIVERY_STATE_DELIVERING_TO_RUNNER, dispatcherpb.DeliveryState_DELIVERY_STATE_DELIVERING_TO_RUNNER},
		{"admitted", dispatcherpb.DeliveryState_DELIVERY_STATE_RUNNER_ADMITTED, dispatcherpb.DeliveryState_DELIVERY_STATE_RUNNER_ADMITTED},
		{"failed", dispatcherpb.DeliveryState_DELIVERY_STATE_FAILED, dispatcherpb.DeliveryState_DELIVERY_STATE_FAILED},
		{"cancelled", dispatcherpb.DeliveryState_DELIVERY_STATE_CANCELLED_IN_DISPATCHER, dispatcherpb.DeliveryState_DELIVERY_STATE_CANCELLED_IN_DISPATCHER},
		{"unknown", dispatcherpb.DeliveryState_DELIVERY_STATE_UNKNOWN, dispatcherpb.DeliveryState_DELIVERY_STATE_UNKNOWN},
		{"reserved former harness value", dispatcherpb.DeliveryState(4), dispatcherpb.DeliveryState_DELIVERY_STATE_UNKNOWN},
		{"reserved former no-op value", dispatcherpb.DeliveryState(5), dispatcherpb.DeliveryState_DELIVERY_STATE_UNKNOWN},
		{"future value", dispatcherpb.DeliveryState(99), dispatcherpb.DeliveryState_DELIVERY_STATE_UNKNOWN},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			if got := NormalizeDeliveryState(test.state); got != test.want {
				t.Fatalf("NormalizeDeliveryState(%d) = %s, want %s", test.state, got, test.want)
			}
		})
	}
}
