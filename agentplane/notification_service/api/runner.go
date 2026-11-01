package api

import (
	"context"
	"time"

	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	environmentruntimepb "github.com/agentydragon/ducktape/agentplane/env/runtime/control/environmentruntimepb"
	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	"github.com/agentydragon/ducktape/agentplane/sandboxes/environmentruntimeclient"
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
	err := environmentruntimeclient.WithClient(ctx, environmentruntimeclient.Config{
		Target: environmentruntimeclient.ExpandTarget(template, sandbox.Name, sandbox.Namespace), TokenFile: r.TokenFile, Timeout: r.Timeout,
		SandboxName: sandbox.Name, SandboxUID: sandbox.UID, SandboxNamespace: sandbox.Namespace,
	}, func(ctx context.Context, client environmentruntimepb.EnvironmentRuntimeClient) error {
		response, err := client.ListSessions(ctx, &sessionpb.ListSessionsRequest{})
		for _, summary := range response.GetSessions() {
			sessions = append(sessions, summary.GetSessionId())
		}
		return err
	})
	return sessions, err
}
