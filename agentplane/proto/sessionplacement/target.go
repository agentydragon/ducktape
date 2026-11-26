// Package sessionplacement reads the environment selected by a Session placement.
package sessionplacement

import sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"

// Target returns the hosted environment or the central sampler's executor environment.
// An absent target remains nil so callers can apply their own admission or display rules.
func Target(placement *sessionpb.SessionPlacement) *sessionpb.ExactEnvironment {
	if hosted := placement.GetEnvironmentHosted(); hosted != nil {
		return hosted.GetEnvironment()
	}
	return placement.GetCentralSamplerV1().GetExecutorEnvironment()
}
