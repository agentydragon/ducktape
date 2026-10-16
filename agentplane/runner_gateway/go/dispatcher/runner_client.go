package dispatcher

import (
	"context"
	"errors"
	"time"

	commandpb "github.com/agentydragon/ducktape/agentplane/proto/command"
	eventlogpb "github.com/agentydragon/ducktape/agentplane/proto/event_log"
	runnerpb "github.com/agentydragon/ducktape/agentplane/proto/runner"
	controlpb "github.com/agentydragon/ducktape/agentplane/proto/runner_control"
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

func (r *ProxyRunner) withClient(ctx context.Context, sandbox SandboxSnapshot, call func(context.Context, controlpb.RunnerClient) error) error {
	if r.Timeout <= 0 {
		return errors.New("runner request timeout must be positive")
	}
	if sandbox.Name == "" || sandbox.UID == "" || sandbox.Namespace == "" || sandbox.Namespace != r.Namespace {
		return errors.New("complete in-namespace Sandbox identity is required for runner proxy")
	}
	return runnerclient.WithClient(ctx, runnerclient.Config{
		Target: r.target(sandbox.Name), TokenFile: r.TokenFile, Timeout: r.Timeout,
		SandboxName: sandbox.Name, SandboxUID: sandbox.UID, SandboxNamespace: sandbox.Namespace,
		DisableRetry: true, ReplaceOutgoingMetadata: true,
	}, call)
}

func (r *ProxyRunner) OpenSession(ctx context.Context, sandbox SandboxSnapshot, sessionID string, spec *runnerpb.SessionSpec, setupScript *string) error {
	return r.withClient(ctx, sandbox, func(ctx context.Context, client controlpb.RunnerClient) error {
		_, err := client.OpenSession(ctx, &controlpb.OpenSessionRequest{
			SessionId: sessionID, Spec: spec, SetupScript: setupScript,
		})
		return err
	})
}

func (r *ProxyRunner) Deliver(ctx context.Context, sandbox SandboxSnapshot, sessionID string, command *commandpb.Command) (*eventlogpb.EventEntry, error) {
	var admission *eventlogpb.EventEntry
	err := r.withClient(ctx, sandbox, func(ctx context.Context, client controlpb.RunnerClient) error {
		resume := func() error {
			_, err := client.ResumeSession(ctx, &controlpb.ResumeSessionRequest{SessionId: sessionID})
			return err
		}
		switch payload := command.GetOperation().(type) {
		case *commandpb.Command_SubmitInput:
			if err := resume(); err != nil {
				return err
			}
			response, err := client.SubmitInput(ctx, &controlpb.SubmitInputRequest{
				SessionId: sessionID, CommandId: command.GetCommandId(), Text: payload.SubmitInput.GetText(),
			})
			if err == nil {
				admission = response.GetAdmission()
			}
			return err
		case *commandpb.Command_ChangeModel:
			if err := resume(); err != nil {
				return err
			}
			response, err := client.ChangeModel(ctx, &controlpb.ChangeModelRequest{
				SessionId: sessionID, CommandId: command.GetCommandId(), Model: payload.ChangeModel.GetModel(),
			})
			if err == nil {
				admission = response.GetAdmission()
			}
			return err
		case *commandpb.Command_ChangeReasoningEffort:
			if err := resume(); err != nil {
				return err
			}
			response, err := client.ChangeReasoningEffort(ctx, &controlpb.ChangeReasoningEffortRequest{
				SessionId: sessionID, CommandId: command.GetCommandId(), Effort: payload.ChangeReasoningEffort.GetEffort(),
			})
			if err == nil {
				admission = response.GetAdmission()
			}
			return err
		case *commandpb.Command_InterruptTurn:
			response, err := client.InterruptTurn(ctx, &controlpb.InterruptTurnRequest{
				SessionId: sessionID, CommandId: command.GetCommandId(), TurnId: payload.InterruptTurn.GetTurnId(),
			})
			if err == nil {
				admission = response.GetAdmission()
			}
			return err
		case *commandpb.Command_StopRunnerSession:
			response, err := client.StopRunnerSession(ctx, &controlpb.StopRunnerSessionRequest{
				SessionId: sessionID, CommandId: command.GetCommandId(),
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
