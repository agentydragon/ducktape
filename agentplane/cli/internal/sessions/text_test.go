package sessions

import (
	"encoding/json"
	"strings"
	"testing"

	dispatcherpb "github.com/agentydragon/ducktape/agentplane/runner_gateway/dispatcher/api/session/session_dispatcherpb"
	"google.golang.org/protobuf/encoding/protojson"
)

func TestSessionLineRejectsIncompleteResource(t *testing.T) {
	for _, raw := range []string{
		`{"sessionId":"session","placement":{"environmentHosted":{"environment":{"name":"sandbox","uid":"uid"}}}}`,
		`{"sessionId":"session","status":{"materialization":"SESSION_MATERIALIZATION_WAITING"}}`,
	} {
		if _, err := sessionLine(json.RawMessage(raw)); err == nil {
			t.Errorf("sessionLine(%s) accepted an incomplete Session", raw)
		}
	}
}

func TestDispatchLineMapsUnknownStateToNamedUnknown(t *testing.T) {
	raw, err := protojson.Marshal(&dispatcherpb.Dispatch{CommandId: "command", State: dispatcherpb.DeliveryState(99)})
	if err != nil {
		t.Fatal(err)
	}
	for _, value := range []json.RawMessage{
		raw,
		json.RawMessage(`{"commandId":"command","state":"DELIVERY_STATE_FUTURE"}`),
	} {
		line, err := dispatchLine(value)
		if err != nil {
			t.Fatal(err)
		}
		if !strings.Contains(line, "unknown") || strings.Contains(line, "99") {
			t.Fatalf("dispatchLine() = %q, want the normalized unknown state", line)
		}
	}
}
