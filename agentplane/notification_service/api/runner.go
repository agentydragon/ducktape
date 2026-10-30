package api

import (
	"context"
	"time"

	runnerpb "github.com/agentydragon/ducktape/agentplane/proto/runner"
	runnerservicepb "github.com/agentydragon/ducktape/agentplane/runner/runner_servicepb"
	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	"github.com/agentydragon/ducktape/agentplane/sandboxes/runnerclient"
	"github.com/agentydragon/ducktape/agentplane/sandboxes/sandboxreader"
)

// ProxyRunner asks a Sandbox's runner through the Sandbox's control Service, as the Session Dispatcher does:
// every call dials the Service afresh, so it reaches the runner's current connection holder.
type ProxyRunner struct {
	// TokenFile is the projected ServiceAccount token the Runner Gateway proxy admits, read on every call.
	TokenFile string
	Timeout   time.Duration
	// TargetTemplate defaults to the per-Sandbox control Service when empty. A test points it at a fixed server.
	TargetTemplate string
}

func (r *ProxyRunner) ListSessions(ctx context.Context, sandbox sandboxreader.Identity) ([]string, error) {
	template := r.TargetTemplate
	if template == "" {
		template = sandboxapi.ControlServiceTargetTemplate()
	}
	var sessions []string
	err := runnerclient.WithClient(ctx, runnerclient.Config{
		Target: runnerclient.ExpandTarget(template, sandbox.Name, sandbox.Namespace), TokenFile: r.TokenFile, Timeout: r.Timeout,
		SandboxName: sandbox.Name, SandboxUID: sandbox.UID, SandboxNamespace: sandbox.Namespace,
	}, func(ctx context.Context, client runnerservicepb.RunnerClient) error {
		response, err := client.ListSessions(ctx, &runnerpb.ListSessionsRequest{})
		for _, summary := range response.GetSessions() {
			sessions = append(sessions, summary.GetSessionId())
		}
		return err
	})
	return sessions, err
}
