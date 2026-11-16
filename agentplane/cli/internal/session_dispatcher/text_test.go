package sessiondispatcher

import (
	"strings"
	"testing"

	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	dispatcherpb "github.com/agentydragon/ducktape/agentplane/session_dispatcher/dispatcher/api/session/session_dispatcherpb"
)

func TestSessionLineRejectsIncompleteResource(t *testing.T) {
	for _, session := range []*dispatcherpb.Session{
		{SessionId: "session", Placement: &sessionpb.SessionPlacement{Kind: &sessionpb.SessionPlacement_EnvironmentHosted{EnvironmentHosted: &sessionpb.EnvironmentHostedPlacement{Environment: &sessionpb.ExactEnvironment{Name: "sandbox", Uid: "uid"}}}}},
		{SessionId: "session", Status: &dispatcherpb.SessionStatus{Materialization: dispatcherpb.SessionStatus_SESSION_MATERIALIZATION_WAITING}},
	} {
		if _, err := sessionLine(session); err == nil {
			t.Errorf("sessionLine(%v) accepted an incomplete Session", session)
		}
	}
}

func TestDispatchLineMapsUnknownStateToNamedUnknown(t *testing.T) {
	line, err := dispatchLine(&dispatcherpb.Dispatch{CommandId: "command", State: dispatcherpb.DeliveryState(99)})
	if err != nil || !strings.Contains(line, "unknown") || strings.Contains(line, "99") {
		t.Fatalf("dispatchLine() = %q, error %v; want the normalized unknown state", line, err)
	}
}
