package sandboxcontroller

import (
	"context"
	"time"

	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
)

// QuiesceResult is a runner's answer to a quiesce.
type QuiesceResult struct {
	// True unless the runner explicitly reports QUIESCE_STAGE_QUIESCED. An absent or unknown stage is
	// not final.
	Quiescing bool
}

// Quiescer asks a Sandbox's runner to seal every Session. A non-nil ifIdleSince makes the request
// conditional: a runner that is busy, or idle since another instant, refuses with `ABORTED` instead of
// beginning to quiesce.
type Quiescer interface {
	Quiesce(ctx context.Context, sandbox *sandboxapi.Sandbox, ifIdleSince *time.Time) (QuiesceResult, error)
}
