package api

import dispatcherpb "github.com/agentydragon/ducktape/agentplane/runner_gateway/dispatcher/session_dispatcherpb"

// openingTarget selects the exact environment used by a native Session or the
// fixed initial executor currently used by a central Session.
func openingTarget(opening *dispatcherpb.SessionOpening, sessionID string) *dispatcherpb.ExactSandbox {
	if opening.GetSessionId() != sessionID {
		return nil
	}
	var target *dispatcherpb.ExactSandbox
	switch opening.GetPlacement().(type) {
	case *dispatcherpb.SessionOpening_EnvironmentHosted:
		if opening.GetInitialExecutor() != nil {
			return nil
		}
		target = opening.GetEnvironmentHosted().GetEnvironment()
	case *dispatcherpb.SessionOpening_CentralSampler:
		target = opening.GetInitialExecutor()
	}
	if target.GetSandbox() == "" || target.GetSandboxUid() == "" {
		return nil
	}
	return target
}
