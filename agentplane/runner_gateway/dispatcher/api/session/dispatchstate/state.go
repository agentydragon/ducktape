package dispatchstate

import dispatcherpb "github.com/agentydragon/ducktape/agentplane/runner_gateway/dispatcher/api/session/session_dispatcherpb"

// NormalizeDeliveryState maps unspecified and unrecognized wire values to the named unknown state.
func NormalizeDeliveryState(state dispatcherpb.DeliveryState) dispatcherpb.DeliveryState {
	if state == dispatcherpb.DeliveryState_DELIVERY_STATE_UNSPECIFIED {
		return dispatcherpb.DeliveryState_DELIVERY_STATE_UNKNOWN
	}
	if _, ok := dispatcherpb.DeliveryState_name[int32(state)]; !ok {
		return dispatcherpb.DeliveryState_DELIVERY_STATE_UNKNOWN
	}
	return state
}
