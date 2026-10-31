package dispatcher

import (
	"context"
	"errors"
	"time"

	commandpb "github.com/agentydragon/ducktape/agentplane/proto/command"
	eventlogpb "github.com/agentydragon/ducktape/agentplane/proto/event_log"
	runnerpb "github.com/agentydragon/ducktape/agentplane/proto/runner"
	runnerservicepb "github.com/agentydragon/ducktape/agentplane/runner/runner_servicepb"
	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	"github.com/agentydragon/ducktape/agentplane/sandboxes/runnerclient"
)

// ProxyRunner dials the per-Sandbox control Service for each call. A new
// connection resolves the holder replica again after a Lease handoff; command
// IDs reconcile uncertain replies at the runner journal.
type ProxyRunner struct {
	Namespace string
	TokenFile string
	Timeout   time.Duration
	// Defaults to the per-Sandbox control Service when empty. Templates can also
	// be a fixed target for host-process integration tests.
	TargetTemplate string
}

func (r *ProxyRunner) target(sandbox string) string {
	template := r.TargetTemplate
	if template == "" {
		template = sandboxapi.ControlServiceTargetTemplate()
	}
	return runnerclient.ExpandTarget(template, sandbox, r.Namespace)
}

func (r *ProxyRunner) withClient(ctx context.Context, sandbox SandboxSnapshot, call func(context.Context, runnerservicepb.RunnerClient) error) error {
	if r.Timeout <= 0 {
		return errors.New("runner request timeout must be positive")
	}
	if sandbox.Name == "" || sandbox.UID == "" || sandbox.Namespace == "" || sandbox.Namespace != r.Namespace {
		return errors.New("complete in-namespace Sandbox identity is required for runner proxy")
	}
	return runnerclient.WithClient(ctx, runnerclient.Config{
		Target: r.target(sandbox.Name), TokenFile: r.TokenFile, Timeout: r.Timeout,
		SandboxName: sandbox.Name, SandboxUID: sandbox.UID, SandboxNamespace: sandbox.Namespace,
		ReplaceOutgoingMetadata: true,
	}, call)
}

func (r *ProxyRunner) OpenSession(ctx context.Context, sandbox SandboxSnapshot, sessionID string, spec *runnerpb.SessionSpec, setupScript *string) error {
	return r.withClient(ctx, sandbox, func(ctx context.Context, client runnerservicepb.RunnerClient) error {
		_, err := client.OpenSession(ctx, &runnerservicepb.OpenSessionRequest{
			SessionId: sessionID, Spec: spec, SetupScript: setupScript,
		})
		return err
	})
}

func (r *ProxyRunner) Deliver(ctx context.Context, sandbox SandboxSnapshot, sessionID string, command *commandpb.Command) (*eventlogpb.EventEntry, error) {
	var admission *eventlogpb.EventEntry
	err := r.withClient(ctx, sandbox, func(ctx context.Context, client runnerservicepb.RunnerClient) error {
		switch payload := command.GetOperation().(type) {
		case *commandpb.Command_SubmitInput:
			response, err := client.SubmitInput(ctx, &runnerservicepb.SubmitInputRequest{
				SessionId: sessionID, CommandId: command.GetCommandId(), Text: payload.SubmitInput.GetText(),
			})
			if err == nil {
				admission = response.GetAdmission()
			}
			return err
		case *commandpb.Command_ChangeModel:
			response, err := client.ChangeModel(ctx, &runnerservicepb.ChangeModelRequest{
				SessionId: sessionID, CommandId: command.GetCommandId(), Model: payload.ChangeModel.GetModel(),
			})
			if err == nil {
				admission = response.GetAdmission()
			}
			return err
		case *commandpb.Command_ChangeReasoningEffort:
			response, err := client.ChangeReasoningEffort(ctx, &runnerservicepb.ChangeReasoningEffortRequest{
				SessionId: sessionID, CommandId: command.GetCommandId(), Effort: payload.ChangeReasoningEffort.GetEffort(),
			})
			if err == nil {
				admission = response.GetAdmission()
			}
			return err
		case *commandpb.Command_InterruptTurn:
			response, err := client.InterruptTurn(ctx, &runnerservicepb.InterruptTurnRequest{
				SessionId: sessionID, CommandId: command.GetCommandId(), TurnId: payload.InterruptTurn.GetTurnId(),
			})
			if err == nil {
				admission = response.GetAdmission()
			}
			return err
		default:
			return errors.New("unsupported runner command")
		}
	})
	return admission, err
}
