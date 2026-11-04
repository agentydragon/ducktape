package api

import (
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	dispatcherpb "github.com/agentydragon/ducktape/agentplane/runner_gateway/dispatcher/session_dispatcherpb"
)

// sessionTarget selects the exact environment used by a native Session or the
// fixed executor currently used by a central Session.
func sessionTarget(session *dispatcherpb.Session, sessionID string) *sessionpb.ExactEnvironment {
	if session.GetSessionId() != sessionID {
		return nil
	}
	placement := session.GetPlacement()
	if placement == nil {
		return nil
	}
	var target *sessionpb.ExactEnvironment
	switch placement.Kind.(type) {
	case *sessionpb.SessionPlacement_EnvironmentHosted:
		target = placement.GetEnvironmentHosted().GetEnvironment()
	case *sessionpb.SessionPlacement_CentralSamplerV1:
		target = placement.GetCentralSamplerV1().GetExecutorEnvironment()
	}
	if target == nil || target.GetName() == "" || target.GetUid() == "" {
		return nil
	}
	return target
}
