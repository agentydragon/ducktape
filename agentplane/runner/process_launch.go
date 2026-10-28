package runner

import (
	"context"

	"github.com/agentydragon/ducktape/agentplane/runner/supervision"
)

// The owning controller has already admitted this operation. Accepted setup and
// bootstrap work still finish if Close has since closed new admissions; their
// operation slots keep shutdown from advancing until they settle.
func (r *Runner) launchProcess(ctx context.Context, owner string, supervisor *supervision.Supervisor, config supervision.ProcessConfig) (*supervision.ManagedProcess, error) {
	generation := r.lifecycle.desiredState().Generation
	return r.lifecycle.processes.Launch(ctx, owner, generation, supervisor, config)
}
