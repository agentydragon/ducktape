package dispatcher

import (
	"context"
	"errors"
	"time"

	"github.com/agentydragon/ducktape/agentplane/grpc/bearer"
	harnesshostpb "github.com/agentydragon/ducktape/agentplane/harness_host/harnesshostpb"
	"github.com/agentydragon/ducktape/agentplane/net/peertls"
	commandpb "github.com/agentydragon/ducktape/agentplane/proto/command"
	eventlogpb "github.com/agentydragon/ducktape/agentplane/proto/event_log"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	"github.com/agentydragon/ducktape/agentplane/sandboxes/environmentruntimeclient"
	"github.com/agentydragon/ducktape/agentplane/session_dispatcher/internal/store"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
)

// HarnessHostClient delivers the same Session protocol to hosted and central harnesses.
// Command IDs reconcile uncertain replies at the selected host.
type HarnessHostClient struct {
	Namespace string
	TokenFile string
	Timeout   time.Duration
	// GatewayTLS pins the Environments server SVID and may present the Sessions role SVID.
	GatewayTLS *peertls.ClientTLS
	// Defaults to the stable Environments Service when empty. Templates can also
	// be a fixed target for host-process integration tests.
	TargetTemplate string
}

func (r *HarnessHostClient) target(sandbox string) string {
	template := r.TargetTemplate
	if template == "" {
		template = sandboxapi.RuntimeProxyTargetTemplate()
	}
	return environmentruntimeclient.ExpandTarget(template, sandbox, r.Namespace)
}

type harnessHostTarget struct {
	address   string
	tokenFile string
	tls       *peertls.ClientTLS
	metadata  metadata.MD
}

func (r *HarnessHostClient) proxyTarget(sandbox SandboxSnapshot) (harnessHostTarget, error) {
	if sandbox.Name == "" || sandbox.UID == "" || sandbox.Namespace == "" || sandbox.Namespace != r.Namespace {
		return harnessHostTarget{}, errors.New("complete in-namespace Sandbox identity is required for HarnessHost proxy")
	}
	return harnessHostTarget{address: r.target(sandbox.Name), tokenFile: r.TokenFile, tls: r.GatewayTLS, metadata: metadata.Pairs(
		sandboxapi.RuntimeProxySandboxMetadataKey, sandbox.Name,
		sandboxapi.RuntimeProxySandboxNamespaceMetadataKey, sandbox.Namespace,
		sandboxapi.RuntimeProxySandboxUIDMetadataKey, sandbox.UID,
		sandboxapi.RuntimeProxySandboxOwnerNamespaceMetadataKey, sandbox.Namespace,
		sandboxapi.RuntimeProxySandboxOwnerNameMetadataKey, sandbox.Name,
	)}, nil
}

func (r *HarnessHostClient) withClient(ctx context.Context, target harnessHostTarget, call func(context.Context, harnesshostpb.HarnessHostClient) error) error {
	if r.Timeout <= 0 {
		return errors.New("HarnessHost request timeout must be positive")
	}
	conn, err := bearer.Dial(target.address, target.tokenFile, r.Timeout, target.tls)
	if err != nil {
		return err
	}
	defer conn.Close()
	ctx, cancel := context.WithTimeout(ctx, r.Timeout)
	defer cancel()
	// Forward only target routing metadata; the transport supplies the service credential.
	return call(metadata.NewOutgoingContext(ctx, target.metadata), harnesshostpb.NewHarnessHostClient(conn))
}

func (r *HarnessHostClient) CreateSession(ctx context.Context, sandbox SandboxSnapshot, sessionID string, spec *sessionpb.SessionSpec, setupScript *string, execution store.SessionExecution) error {
	target, err := r.proxyTarget(sandbox)
	if err != nil {
		return err
	}
	return r.createSession(ctx, target, sessionID, spec, setupScript, execution)
}

func (r *HarnessHostClient) createSession(ctx context.Context, target harnessHostTarget, sessionID string, spec *sessionpb.SessionSpec, setupScript *string, execution store.SessionExecution) error {
	return r.withClient(ctx, target, func(ctx context.Context, client harnesshostpb.HarnessHostClient) error {
		_, err := client.CreateSession(ctx, &harnesshostpb.CreateSessionRequest{
			SessionId: sessionID, Spec: spec, SetupScript: setupScript,
			ExecutionIdentity: execution.Identity,
		})
		return err
	})
}

func (r *HarnessHostClient) Deliver(ctx context.Context, sandbox SandboxSnapshot, sessionID string, command *commandpb.Command) (*eventlogpb.EventEntry, error) {
	target, err := r.proxyTarget(sandbox)
	if err != nil {
		return nil, err
	}
	return r.deliver(ctx, target, sessionID, command)
}

func (r *HarnessHostClient) deliver(ctx context.Context, target harnessHostTarget, sessionID string, command *commandpb.Command) (*eventlogpb.EventEntry, error) {
	var admission *eventlogpb.EventEntry
	err := r.withClient(ctx, target, func(ctx context.Context, client harnesshostpb.HarnessHostClient) error {
		switch payload := command.GetOperation().(type) {
		case *commandpb.Command_SubmitInput:
			response, err := client.SubmitInput(ctx, &harnesshostpb.SubmitInputRequest{
				SessionId: sessionID, CommandId: command.GetCommandId(), Text: payload.SubmitInput.GetText(),
			})
			if err == nil {
				admission = response.GetAdmission()
			}
			return err
		case *commandpb.Command_ChangeModel:
			response, err := client.ChangeModel(ctx, &harnesshostpb.ChangeModelRequest{
				SessionId: sessionID, CommandId: command.GetCommandId(), Model: payload.ChangeModel.GetModel(),
			})
			if err == nil {
				admission = response.GetAdmission()
			}
			return err
		case *commandpb.Command_ChangeReasoningEffort:
			response, err := client.ChangeReasoningEffort(ctx, &harnesshostpb.ChangeReasoningEffortRequest{
				SessionId: sessionID, CommandId: command.GetCommandId(), Effort: payload.ChangeReasoningEffort.GetEffort(),
			})
			if err == nil {
				admission = response.GetAdmission()
			}
			return err
		case *commandpb.Command_InterruptTurn:
			response, err := client.InterruptTurn(ctx, &harnesshostpb.InterruptTurnRequest{
				SessionId: sessionID, CommandId: command.GetCommandId(), TurnId: payload.InterruptTurn.GetTurnId(),
			})
			if err == nil {
				admission = response.GetAdmission()
			}
			return err
		default:
			return status.Error(codes.InvalidArgument, "unsupported HarnessHost command")
		}
	})
	return admission, err
}
