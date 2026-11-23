package dispatcher

import (
	"context"

	"github.com/agentydragon/ducktape/agentplane/net/peertls"
	commandpb "github.com/agentydragon/ducktape/agentplane/proto/command"
	eventlogpb "github.com/agentydragon/ducktape/agentplane/proto/event_log"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	"github.com/agentydragon/ducktape/agentplane/session_dispatcher/internal/placement"
	"github.com/agentydragon/ducktape/agentplane/session_dispatcher/internal/store"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

// CentralTarget is deployment-owned. The Dispatcher selects the service for new sessions.
type CentralTarget struct {
	Target    string             `mapstructure:"target"`
	TokenFile string             `mapstructure:"token_file"`
	TLS       *peertls.ClientTLS `mapstructure:"tls"`
}

type sessionReader interface {
	GetSession(context.Context, string) (*store.Session, error)
}

// SessionDeliveryRouter keeps Environment-hosted execution local and routes centrally persisted
// Sessions directly to their sampler. The execution Sandbox is a binding,
// not the authority for the central conversation's lifetime.
type SessionDeliveryRouter struct {
	Host   *HarnessHostClient
	Store  sessionReader
	Target *CentralTarget
}

func validateSessionExecutor(session *store.Session, sandbox SandboxSnapshot) error {
	if session.SandboxName != sandbox.Name || session.SandboxUID != sandbox.UID {
		return status.Error(codes.FailedPrecondition, "session executor identity changed")
	}
	return nil
}

func ValidateRuntime(sessionPlacement placement.SessionPlacement, setup *string, target *CentralTarget) error {
	if sessionPlacement != placement.CentralSampler {
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

func (r *SessionDeliveryRouter) hostTarget(sessionPlacement placement.SessionPlacement, sandbox SandboxSnapshot) (harnessHostTarget, error) {
	if sessionPlacement == placement.EnvironmentHosted {
		return r.Host.proxyTarget(sandbox)
	}
	if r.Target == nil {
		return harnessHostTarget{}, status.Error(codes.FailedPrecondition, "central sampler service is not enabled")
	}
	if r.Target.Target == "" {
		return harnessHostTarget{}, status.Error(codes.Internal, "invalid configured central sampler target")
	}
	return harnessHostTarget{address: r.Target.Target, tokenFile: r.Target.TokenFile, tls: r.Target.TLS}, nil
}

func (r *SessionDeliveryRouter) CreateSession(ctx context.Context, sandbox SandboxSnapshot, id string, spec *sessionpb.SessionSpec, setup *string, execution store.SessionExecution) error {
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
	target, err := r.hostTarget(session.Placement, sandbox)
	if err != nil {
		return err
	}
	return r.Host.createSession(ctx, target, id, spec, setup, execution)
}

func (r *SessionDeliveryRouter) Deliver(ctx context.Context, sandbox SandboxSnapshot, id string, command *commandpb.Command) (*eventlogpb.EventEntry, error) {
	session, err := r.Store.GetSession(ctx, id)
	if err != nil {
		return nil, err
	}
	if err := validateSessionExecutor(session, sandbox); err != nil {
		return nil, err
	}
	target, err := r.hostTarget(session.Placement, sandbox)
	if err != nil {
		return nil, err
	}
	return r.Host.deliver(ctx, target, id, command)
}
