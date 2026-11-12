package dispatcher

import (
	"context"
	"time"

	identitypb "github.com/agentydragon/ducktape/agentplane/proto/identity"

	samplerpb "github.com/agentydragon/ducktape/agentplane/central_sampler/samplerpb"
	environmentruntimepb "github.com/agentydragon/ducktape/agentplane/env/runtime/control/environmentruntimepb"
	"github.com/agentydragon/ducktape/agentplane/grpc/bearer"
	commandpb "github.com/agentydragon/ducktape/agentplane/proto/command"
	eventlogpb "github.com/agentydragon/ducktape/agentplane/proto/event_log"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
)

// CentralTarget is deployment-owned. The Dispatcher selects the service for new sessions.
type CentralTarget struct {
	Target    string `mapstructure:"target"`
	TokenFile string `mapstructure:"token_file"`
}

// SessionExecution is the exact Identity incarnation a Session executes as.
type SessionExecution struct{ Identity *identitypb.Identity }

func hasExecutionIdentity(session *Session) bool {
	return session != nil && session.ExecutionIdentity != nil && session.ExecutionIdentity.GetName() != "" && session.ExecutionIdentity.GetUid() != ""
}

func (s *Session) execution() SessionExecution {
	if !hasExecutionIdentity(s) {
		return SessionExecution{}
	}
	return SessionExecution{Identity: proto.Clone(s.ExecutionIdentity).(*identitypb.Identity)}
}

type sessionReader interface {
	GetSession(context.Context, string) (*Session, error)
}

// RoutingRunner keeps native execution local and routes centrally persisted
// conversations directly to their sampler. The execution Sandbox is a binding,
// not the authority for the central conversation's lifetime.
type RoutingRunner struct {
	Native  DeliveryRunner
	Store   sessionReader
	Target  *CentralTarget
	Timeout time.Duration
}

func validateSessionExecutor(session *Session, sandbox SandboxSnapshot) error {
	if session.SandboxName != sandbox.Name || session.SandboxUID != sandbox.UID {
		return status.Error(codes.FailedPrecondition, "session executor identity changed")
	}
	return nil
}

func ValidateRuntime(placement SessionPlacement, setup *string, target *CentralTarget) error {
	if placement != CentralSampler {
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
	conn, err := bearer.Dial(target.Target, target.TokenFile, r.Timeout, nil)
	if err != nil {
		return err
	}
	defer conn.Close()
	ctx, cancel := context.WithTimeout(ctx, r.Timeout)
	defer cancel()
	// The sampler sees only the bearer, never metadata the Dispatcher's caller attached.
	return call(metadata.NewOutgoingContext(ctx, metadata.MD{}), samplerpb.NewSamplerClient(conn))
}

func (r *RoutingRunner) CreateSession(ctx context.Context, sandbox SandboxSnapshot, id string, spec *sessionpb.SessionSpec, setup *string, execution SessionExecution) error {
	if execution.Identity == nil || execution.Identity.GetName() == "" || execution.Identity.GetUid() == "" {
		return status.Error(codes.FailedPrecondition, "central Session requires a complete execution Identity")
	}
	session, err := r.Store.GetSession(ctx, id)
	if err != nil {
		return err
	}
	if err := validateSessionExecutor(session, sandbox); err != nil {
		return err
	}
	if err := ValidateRuntime(session.Placement, setup, r.Target); err != nil {
		return err
	}
	if session.Placement == EnvironmentHosted {
		return r.Native.CreateSession(ctx, sandbox, id, spec, setup, execution)
	}
	if sandbox.Incarnation == "" {
		return status.Error(codes.FailedPrecondition, "executor incarnation is not available")
	}
	return r.withClient(ctx, func(ctx context.Context, client samplerpb.SamplerClient) error {
		_, err := client.CreateSession(ctx, &samplerpb.CreateSessionRequest{
			SessionId: id, Spec: spec, ExpectedIncarnationUid: sandbox.Incarnation, SetupScript: setup,
			ExecutionIdentity: execution.Identity,
			Executor:          &identitypb.SandboxDestination{Owner: &identitypb.ServiceAccount{Namespace: sandbox.Namespace, Name: sandbox.Name}, Target: &identitypb.EnvironmentTarget{Namespace: sandbox.Namespace, Name: sandbox.Name, Uid: sandbox.UID}},
		})
		return err
	})
}

func (r *RoutingRunner) Deliver(ctx context.Context, sandbox SandboxSnapshot, id string, command *commandpb.Command) (*eventlogpb.EventEntry, error) {
	session, err := r.Store.GetSession(ctx, id)
	if err != nil {
		return nil, err
	}
	if err := validateSessionExecutor(session, sandbox); err != nil {
		return nil, err
	}
	if session.Placement == EnvironmentHosted {
		return r.Native.Deliver(ctx, sandbox, id, command)
	}
	var admission *eventlogpb.EventEntry
	err = r.withClient(ctx, func(ctx context.Context, client samplerpb.SamplerClient) error {
		switch payload := command.GetOperation().(type) {
		case *commandpb.Command_SubmitInput:
			reply, err := client.SubmitInput(ctx, &environmentruntimepb.SubmitInputRequest{SessionId: id, CommandId: command.GetCommandId(), Text: payload.SubmitInput.GetText()})
			if err == nil {
				admission = reply.GetAdmission()
			}
			return err
		case *commandpb.Command_ChangeModel:
			reply, err := client.ChangeModel(ctx, &environmentruntimepb.ChangeModelRequest{SessionId: id, CommandId: command.GetCommandId(), Model: payload.ChangeModel.GetModel()})
			if err == nil {
				admission = reply.GetAdmission()
			}
			return err
		case *commandpb.Command_ChangeReasoningEffort:
			reply, err := client.ChangeReasoningEffort(ctx, &environmentruntimepb.ChangeReasoningEffortRequest{SessionId: id, CommandId: command.GetCommandId(), Effort: payload.ChangeReasoningEffort.GetEffort()})
			if err == nil {
				admission = reply.GetAdmission()
			}
			return err
		case *commandpb.Command_InterruptTurn:
			reply, err := client.InterruptTurn(ctx, &environmentruntimepb.InterruptTurnRequest{SessionId: id, CommandId: command.GetCommandId(), TurnId: payload.InterruptTurn.GetTurnId()})
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
