package api

import (
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	dispatcherpb "github.com/agentydragon/ducktape/agentplane/runner_gateway/dispatcher/session_dispatcherpb"
)

// openingTarget selects the exact environment used by a native Session or the
// fixed initial executor currently used by a central Session.
func openingTarget(opening *dispatcherpb.SessionOpening, sessionID string) *sessionpb.ExactEnvironment {
	if opening.GetSessionId() != sessionID {
		return nil
	}
	var target *sessionpb.ExactEnvironment
	switch opening.GetPlacement().(type) {
	case *sessionpb.SessionPlacement_EnvironmentHosted:
		target = opening.GetPlacement().GetEnvironmentHosted().GetEnvironment()
	case *sessionpb.SessionPlacement_CentralSamplerV1:
		target = opening.GetPlacement().GetCentralSamplerV1().GetExecutorEnvironment()
	}
	if target.GetName() == "" || target.GetUid() == "" {
		return nil
	}
	return target
}
