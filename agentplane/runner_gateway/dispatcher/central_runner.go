package dispatcher

import (
	"context"
	"errors"
	"time"

	"github.com/agentydragon/ducktape/agentplane/grpc/bearer"
	commandpb "github.com/agentydragon/ducktape/agentplane/proto/command"
	eventlogpb "github.com/agentydragon/ducktape/agentplane/proto/event_log"
	runnerpb "github.com/agentydragon/ducktape/agentplane/proto/runner"
	sandboxpb "github.com/agentydragon/ducktape/agentplane/proto/sandboxes"
	runnerservicepb "github.com/agentydragon/ducktape/agentplane/runner/runner_servicepb"
	samplerpb "github.com/agentydragon/ducktape/agentplane/x/central_sampler/samplerpb"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
)

// CentralTarget is deployment-owned. The Dispatcher selects the service for new sessions.
type CentralTarget struct {
	Target    string `mapstructure:"target"`
	TokenFile string `mapstructure:"token_file"`
}

type openingReader interface {
	GetOpening(context.Context, string) (*Opening, error)
}

// RoutingRunner keeps native execution local and routes centrally persisted
// conversations directly to their sampler. The execution Sandbox is a binding,
// not the authority for the central conversation's lifetime.
type RoutingRunner struct {
	Native  DeliveryRunner
	Store   openingReader
	Target  *CentralTarget
	Timeout time.Duration
}

func centralSpec(spec *runnerpb.SessionSpec) bool {
	return spec.GetHarness() == runnerpb.Harness_HARNESS_CENTRAL_SAMPLER
}

func sessionRequiresReadyExecutor(spec *runnerpb.SessionSpec) bool {
	return !centralSpec(spec)
}

func validateOpeningExecutor(opening *Opening, sandbox SandboxSnapshot) error {
	if opening.SandboxName != sandbox.Name || opening.SandboxUID != sandbox.UID {
		return status.Error(codes.FailedPrecondition, "session executor identity changed")
	}
	return nil
}

func ValidateRuntime(spec *runnerpb.SessionSpec, setup *string, target *CentralTarget) error {
	if !centralSpec(spec) {
		return nil
	}
	if target == nil {
		return status.Error(codes.FailedPrecondition, "central sampler is not configured")
	}
	if setup != nil && len(*setup) > 65536 {
		return status.Error(codes.InvalidArgument, "setup_script exceeds 65536 UTF-8 bytes")
	}
	return nil
}

func (r *RoutingRunner) withClient(ctx context.Context, call func(context.Context, samplerpb.SamplerClient) error) error {
	target := r.Target
	if target == nil {
		return status.Error(codes.FailedPrecondition, "central sampler service is not enabled")
	}
	if target.Target == "" || target.TokenFile == "" || r.Timeout <= 0 {
		return status.Error(codes.Internal, "invalid configured central sampler target")
	}
	conn, err := bearer.Dial(target.Target, target.TokenFile, r.Timeout)
	if err != nil {
		return err
	}
	defer conn.Close()
	ctx, cancel := context.WithTimeout(ctx, r.Timeout)
	defer cancel()
	// The sampler sees only the bearer, never metadata the Dispatcher's caller attached.
	return call(metadata.NewOutgoingContext(ctx, metadata.MD{}), samplerpb.NewSamplerClient(conn))
}

func (r *RoutingRunner) OpenSession(ctx context.Context, sandbox SandboxSnapshot, id string, spec *runnerpb.SessionSpec, setup *string) error {
	if err := ValidateRuntime(spec, setup, r.Target); err != nil {
		return err
	}
	if !centralSpec(spec) {
		return r.Native.OpenSession(ctx, sandbox, id, spec, setup)
	}
	if sandbox.Incarnation == "" {
		return status.Error(codes.FailedPrecondition, "executor incarnation is not available")
	}
	return r.withClient(ctx, func(ctx context.Context, client samplerpb.SamplerClient) error {
		_, err := client.OpenSession(ctx, &samplerpb.OpenSessionRequest{
			SessionId: id, Spec: spec, ExpectedIncarnationUid: sandbox.Incarnation, SetupScript: setup,
			Executor: &sandboxpb.SandboxDestination{Owner: &sandboxpb.ServiceAccount{Namespace: sandbox.Namespace, Name: sandbox.Name}, Sandbox: sandbox.Name, SandboxUid: sandbox.UID},
		})
		return err
	})
}

func (r *RoutingRunner) Deliver(ctx context.Context, sandbox SandboxSnapshot, id string, command *commandpb.Command) (*eventlogpb.EventEntry, error) {
	opening, err := r.Store.GetOpening(ctx, id)
	// Existing runner Sessions may predate Dispatcher OpenSession.
	if errors.Is(err, ErrNotFound) {
		return r.Native.Deliver(ctx, sandbox, id, command)
	}
	if err != nil {
		return nil, err
	}
	if !centralSpec(opening.Spec) {
		return r.Native.Deliver(ctx, sandbox, id, command)
	}
	if err := validateOpeningExecutor(opening, sandbox); err != nil {
		return nil, err
	}
	var admission *eventlogpb.EventEntry
	err = r.withClient(ctx, func(ctx context.Context, client samplerpb.SamplerClient) error {
		switch payload := command.GetOperation().(type) {
		case *commandpb.Command_SubmitInput:
			reply, err := client.SubmitInput(ctx, &runnerservicepb.SubmitInputRequest{SessionId: id, CommandId: command.GetCommandId(), Text: payload.SubmitInput.GetText()})
			if err == nil {
				admission = reply.GetAdmission()
			}
			return err
		case *commandpb.Command_ChangeModel:
			reply, err := client.ChangeModel(ctx, &runnerservicepb.ChangeModelRequest{SessionId: id, CommandId: command.GetCommandId(), Model: payload.ChangeModel.GetModel()})
			if err == nil {
				admission = reply.GetAdmission()
			}
			return err
		case *commandpb.Command_ChangeReasoningEffort:
			reply, err := client.ChangeReasoningEffort(ctx, &runnerservicepb.ChangeReasoningEffortRequest{SessionId: id, CommandId: command.GetCommandId(), Effort: payload.ChangeReasoningEffort.GetEffort()})
			if err == nil {
				admission = reply.GetAdmission()
			}
			return err
		case *commandpb.Command_InterruptTurn:
			reply, err := client.InterruptTurn(ctx, &runnerservicepb.InterruptTurnRequest{SessionId: id, CommandId: command.GetCommandId(), TurnId: payload.InterruptTurn.GetTurnId()})
			if err == nil {
				admission = reply.GetAdmission()
			}
			return err
		default:
			return status.Error(codes.InvalidArgument, "unsupported central sampler command")
		}
	})
	return admission, err
}
