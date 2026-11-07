// Package bridge adapts the app's named Sandbox and thread contracts to the
// Session Dispatcher and the per-Sandbox Runner Gateway.
package bridge

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"time"
	"unicode/utf8"

	"github.com/agentydragon/ducktape/agentplane/environment_runtime_transport"

	identitypb "github.com/agentydragon/ducktape/agentplane/proto/identity"

	"google.golang.org/grpc"

	environmentpb "github.com/agentydragon/ducktape/agentplane/environments/v1"
	"github.com/agentydragon/ducktape/agentplane/observability"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	dispatcherpb "github.com/agentydragon/ducktape/agentplane/runner_gateway/dispatcher/session_dispatcherpb"
	"github.com/google/uuid"
	"google.golang.org/protobuf/encoding/protojson"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/reflect/protoreflect"
)

// SandboxGetter returns the caller-authorized descriptor for an exact Sandbox incarnation.
type SandboxGetter interface {
	GetEnvironment(context.Context, *environmentpb.GetEnvironmentRequest, ...grpc.CallOption) (*environmentpb.EnvironmentDescriptor, error)
}

// Dispatcher is the narrow unary surface used by the RunnerBridge.
type Dispatcher interface {
	CreateSession(context.Context, *dispatcherpb.CreateSessionRequest) (*dispatcherpb.CreateSessionResponse, error)
}

// RunnerProxy performs informational calls through the Sandbox's control
// Service. The concrete GatewayRunnerProxy binds all four identity values on
// every call.
type RunnerProxy interface {
	ListSessions(context.Context, *environmentpb.EnvironmentDescriptor) (*sessionpb.ListSessionsResponse, error)
	EnvironmentRuntimeStatus(context.Context, *environmentpb.EnvironmentDescriptor) (*sessionpb.EnvironmentRuntimeStatus, error)
}

// RunnerBridge joins the app's Sandbox identity, Dispatcher and Runner services.
type RunnerBridge struct {
	namespace  string
	sandboxes  SandboxGetter
	runner     RunnerProxy
	dispatcher Dispatcher
}

// New constructs the bridge. Runner may be nil for callers using only Sessions.
func New(namespace string, sandboxes SandboxGetter, runner RunnerProxy, dispatcher Dispatcher) (*RunnerBridge, error) {
	if namespace == "" || sandboxes == nil || dispatcher == nil {
		return nil, errors.New("Sandbox getter and Session Dispatcher are required")
	}
	return &RunnerBridge{namespace: namespace, sandboxes: sandboxes, runner: runner, dispatcher: dispatcher}, nil
}

// CreateSession queues one immutable Session specification. The caller-minted
// UUID is the dispatcher's idempotency key, so a timeout is returned with that
// same ID for reconciliation or retry.
func (b *RunnerBridge) CreateSession(
	ctx context.Context, sandbox, sandboxUID string, sessionID uuid.UUID, spec *sessionpb.SessionSpec, setupScript *string,
) (*dispatcherpb.Session, error) {
	if sandbox == "" || sandboxUID == "" || spec == nil {
		return nil, errors.New("Sandbox name, UID and Session spec are required")
	}
	if setupScript != nil && utf8.RuneCountInString(*setupScript) > MaxSetupScriptCharacters {
		return nil, fmt.Errorf("setup script exceeds %d characters", MaxSetupScriptCharacters)
	}
	request := &dispatcherpb.CreateSessionRequest{
		SessionId: sessionID.String(), Spec: proto.Clone(spec).(*sessionpb.SessionSpec),
	}
	target := spec.GetPlacement().GetEnvironmentHosted().GetEnvironment()
	if spec.GetPlacement().GetCentralSamplerV1() != nil {
		target = spec.GetPlacement().GetCentralSamplerV1().GetExecutorEnvironment()
	}
	if target.GetName() != sandbox || target.GetUid() != sandboxUID {
		return nil, errors.New("Session placement does not match requested Sandbox")
	}
	if setupScript != nil {
		value := *setupScript
		request.SetupScript = &value
	}
	ctx, span := observability.Start(ctx, "agentplane.dispatcher.create_session")
	response, err := b.dispatcher.CreateSession(ctx, request)
	observability.End(span, err)
	if err != nil {
		var timeout *RequestTimeoutError
		if errors.As(err, &timeout) {
			return nil, &DispatcherRequestTimeoutError{SessionID: sessionID, Cause: err}
		}
		return nil, err
	}
	session := response.GetSession()
	var returnedTarget *sessionpb.ExactEnvironment
	if session != nil {
		returnedTarget = session.GetPlacement().GetEnvironmentHosted().GetEnvironment()
		if session.GetPlacement().GetCentralSamplerV1() != nil {
			returnedTarget = session.GetPlacement().GetCentralSamplerV1().GetExecutorEnvironment()
		}
	}
	if session == nil || session.GetSessionId() != sessionID.String() || returnedTarget.GetName() != sandbox ||
		returnedTarget.GetUid() != sandboxUID {
		return nil, &SessionNotFoundError{SessionID: sessionID, Sandbox: sandbox}
	}
	return session, nil
}

// ListSessions proxies the live runner's durable Session inventory through the requested Sandbox incarnation.
func (b *RunnerBridge) ListSessions(ctx context.Context, sandboxName, sandboxUID string) ([]*sessionpb.SessionSummary, error) {
	if b.runner == nil {
		return nil, errors.New("Runner proxy is not configured")
	}
	sandbox, err := b.currentSandbox(ctx, sandboxName, sandboxUID)
	if err != nil {
		return nil, err
	}
	response, err := b.runner.ListSessions(ctx, sandbox)
	if err != nil {
		return nil, err
	}
	return response.GetSessions(), nil
}

// EnvironmentRuntimeStatus returns the live status snapshot for the requested Sandbox
// incarnation. The snapshot is informational and does not wake the Sandbox.
func (b *RunnerBridge) EnvironmentRuntimeStatus(ctx context.Context, sandboxName, sandboxUID string) (*sessionpb.EnvironmentRuntimeStatus, error) {
	if b.runner == nil {
		return nil, errors.New("Runner proxy is not configured")
	}
	sandbox, err := b.currentSandbox(ctx, sandboxName, sandboxUID)
	if err != nil {
		return nil, err
	}
	return b.runner.EnvironmentRuntimeStatus(ctx, sandbox)
}

func (b *RunnerBridge) currentSandbox(ctx context.Context, name, uid string) (*environmentpb.EnvironmentDescriptor, error) {
	if name == "" || uid == "" {
		return nil, errors.New("Sandbox name and UID are required")
	}
	sandbox, err := b.sandboxes.GetEnvironment(ctx, &environmentpb.GetEnvironmentRequest{Target: &identitypb.EnvironmentTarget{Namespace: b.namespace, Name: name, Uid: uid}})
	if err != nil {
		return nil, err
	}
	if sandbox == nil || sandbox.GetTarget() == nil || sandbox.GetTarget().GetName() != name ||
		sandbox.GetTarget().GetUid() != uid || sandbox.GetTarget().GetNamespace() != b.namespace {
		return nil, fmt.Errorf("Sandbox %q has incomplete or mismatched identity", name)
	}
	return sandbox, nil
}

// ParseSessionSpec applies strict proto-JSON fields over a cloned set of
// defaults. Explicit empty values override defaults, and unknown fields fail.
func ParseSessionSpec(defaults *sessionpb.SessionSpec, fields map[string]any) (*sessionpb.SessionSpec, error) {
	if defaults == nil {
		defaults = &sessionpb.SessionSpec{}
	}
	merged := proto.Clone(defaults).(*sessionpb.SessionSpec)
	encoded, err := json.Marshal(fields)
	if err != nil {
		return nil, &MalformedMessageError{Cause: err}
	}
	partial := &sessionpb.SessionSpec{}
	if err := (protojson.UnmarshalOptions{DiscardUnknown: false}).Unmarshal(encoded, partial); err != nil {
		return nil, &MalformedMessageError{Cause: err}
	}
	// protojson.Unmarshal resets its destination, while the Python ParseDict
	// contract merges fields into the preset. Copy only keys present in the
	// request, including explicitly supplied zero values such as "model": "".
	descriptor := merged.ProtoReflect().Descriptor().Fields()
	for name := range fields {
		field := descriptor.ByJSONName(name)
		if field == nil {
			field = descriptor.ByName(protoreflect.Name(name))
		}
		if field != nil {
			merged.ProtoReflect().Set(field, partial.ProtoReflect().Get(field))
		}
	}
	return merged, nil
}

// RunnerConfig configures per-call control Service connections.
type RunnerConfig struct {
	TLS            *environment_runtime_transport.ClientTLS
	Namespace      string
	TokenFile      string
	Timeout        time.Duration
	TargetTemplate string
}

const MaxSetupScriptCharacters = 65_536
