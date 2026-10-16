package dispatcher

import (
	"context"
	"errors"
	"os"
	"strings"
	"time"

	commandpb "github.com/agentydragon/ducktape/agentplane/proto/command"
	eventlogpb "github.com/agentydragon/ducktape/agentplane/proto/event_log"
	runnerpb "github.com/agentydragon/ducktape/agentplane/proto/runner"
	controlpb "github.com/agentydragon/ducktape/agentplane/proto/runner_control"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/metadata"
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
		template = "{sandbox}-control.{namespace}.svc:8080"
	}
	target := strings.ReplaceAll(template, "{sandbox}", sandbox)
	return strings.ReplaceAll(target, "{namespace}", r.Namespace)
}

func (r *ProxyRunner) withClient(ctx context.Context, sandbox SandboxSnapshot, call func(context.Context, controlpb.RunnerClient) error) error {
	if r.Timeout <= 0 {
		return errors.New("runner request timeout must be positive")
	}
	data, err := os.ReadFile(r.TokenFile)
	if err != nil {
		return err
	}
	token := strings.TrimSpace(string(data))
	if token == "" {
		return errors.New("runner proxy token is empty")
	}
	if sandbox.Name == "" || sandbox.UID == "" || sandbox.Namespace == "" || sandbox.Namespace != r.Namespace {
		return errors.New("complete in-namespace Sandbox identity is required for runner proxy")
	}
	conn, err := grpc.NewClient(r.target(sandbox.Name), grpc.WithTransportCredentials(insecure.NewCredentials()), grpc.WithDisableRetry())
	if err != nil {
		return err
	}
	defer conn.Close()
	ctx, cancel := context.WithTimeout(ctx, r.Timeout)
	defer cancel()
	ctx = metadata.NewOutgoingContext(ctx, metadata.Pairs("authorization", "Bearer "+token,
		"x-agentplane-sandbox", sandbox.Name, "x-agentplane-sandbox-uid", sandbox.UID,
		"x-agentplane-sandbox-owner-namespace", sandbox.Namespace, "x-agentplane-sandbox-owner-name", sandbox.Name))
	return call(ctx, controlpb.NewRunnerClient(conn))
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
