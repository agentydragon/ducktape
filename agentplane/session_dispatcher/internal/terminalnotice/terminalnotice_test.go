package terminalnotice

import (
	"strings"
	"testing"

	dispatcherpb "github.com/agentydragon/ducktape/agentplane/session_dispatcher/dispatcher/api/session/session_dispatcherpb"
	"google.golang.org/protobuf/proto"
)

func TestTerminalPayloadRoundTripThroughAssembler(t *testing.T) {
	reason := "sandbox_deleted"
	event := Event{ID: "00000000-0000-4000-8000-000000000123", Dispatch: &dispatcherpb.Dispatch{
		CommandId: "terminal", Sandbox: strings.Repeat("sandbox", 1200),
		State: dispatcherpb.DeliveryState_DELIVERY_STATE_FAILED, Reason: &reason,
	}}
	payloads, err := Encode(event)
	if err != nil {
		t.Fatal(err)
	}
	if len(payloads) < 2 {
		t.Fatalf("large terminal snapshot did not span multiple NOTIFY payloads: %d", len(payloads))
	}
	var assembler Assembler
	var received *Event
	// Reassembly must tolerate parts arriving out of order across writers or reconnects.
	for i := len(payloads) - 1; i >= 0; i-- {
		if len(payloads[i]) > MaxPayloadBytes {
			t.Fatalf("part %d exceeds PostgreSQL NOTIFY limit", i)
		}
		var err error
		received, err = assembler.Add(payloads[i])
		if err != nil {
			t.Fatal(err)
		}
	}
	if received == nil || received.ID != event.ID || !proto.Equal(received.Dispatch, event.Dispatch) {
		t.Fatalf("wrong terminal event %+v", received)
	}
}
