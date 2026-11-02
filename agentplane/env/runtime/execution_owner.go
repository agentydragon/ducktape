package runtime

import executorpb "github.com/agentydragon/ducktape/agentplane/env/runtime/execution/executorpb"

// Runtime.mu guards admission and replacement. The handle and incarnation are
// immutable for this lifetime; an unproved fence retains the whole owner.
// Native handles stay outside the COSI resources that describe their outcomes.
type executionLifetime struct {
	handle   executionHandle
	uid      string
	admitted bool
}

func (o *executionLifetime) recovering() bool { return o != nil && !o.admitted }

func (o *executionLifetime) executor(uid string) executorpb.ExecutorServer {
	if o == nil || !o.admitted || o.uid != uid {
		return nil
	}
	if service, ok := o.handle.(*executionServiceHandle); ok && !service.serving() {
		return nil
	}
	if stopping, ok := o.handle.(interface{ stopRequested() bool }); ok && stopping.stopRequested() {
		return nil
	}
	return o.handle.Executor()
}
