package runtime

import (
	"context"

	"github.com/agentydragon/ducktape/agentplane/env/cosi"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/lifecycle"
)

// Caller holds Runtime.mu. Publishing never calls back into the Host mutex.
func (r *Runtime) publishHostIntentLocked() error {
	if r.lifecycleState == nil || r.intent == nil {
		return nil
	}
	value := lifecycle.HostIntentFromProto(r.intent)
	return cosi.Publish(context.Background(), r.lifecycleState, lifecycle.HostIntentResource(value), value, lifecycle.EnvironmentOwner)
}

// Caller holds Runtime.mu. Revocation cannot depend on COSI store availability.
func (r *Runtime) revokeHostConnectionLocked() {
	if r.host != nil && r.host.client != nil {
		r.host.client.SetRuntimeConnection(r.bootID, r.host.bootID, false)
	}
}
