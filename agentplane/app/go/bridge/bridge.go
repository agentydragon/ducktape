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

	"github.com/agentydragon/ducktape/agentplane/observability"
	sandboxespb "github.com/agentydragon/ducktape/agentplane/proto/sandboxes"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	dispatcherpb "github.com/agentydragon/ducktape/agentplane/runner_gateway/dispatcher/session_dispatcherpb"
	"github.com/google/uuid"
	"google.golang.org/protobuf/encoding/protojson"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/reflect/protoreflect"
)

// SandboxGetter returns the caller-visible Sandbox record. Implementations must
// read the current Kubernetes object, including its current UID and namespace.
type SandboxGetter interface {
	Get(context.Context, string) (*sandboxespb.Sandbox, error)
}

// Dispatcher is the narrow unary surface used by the RunnerBridge.
type Dispatcher interface {
	OpenSession(context.Context, *dispatcherpb.OpenSessionRequest) (*dispatcherpb.OpenSessionResponse, error)
	GetSessionOpening(context.Context, *dispatcherpb.GetSessionOpeningRequest) (*dispatcherpb.GetSessionOpeningResponse, error)
}

// RunnerProxy performs informational calls through the Sandbox's control
// Service. The concrete GatewayRunnerProxy binds all four identity values on
// every call.
type RunnerProxy interface {
	ListSessions(context.Context, *sandboxespb.Sandbox) (*sessionpb.ListSessionsResponse, error)
	EnvironmentRuntimeStatus(context.Context, *sandboxespb.Sandbox) (*sessionpb.EnvironmentRuntimeStatus, error)
}

// RunnerBridge joins the app's Sandbox identity, Dispatcher and Runner services.
type RunnerBridge struct {
	sandboxes  SandboxGetter
	runner     RunnerProxy
	dispatcher Dispatcher
}

// New constructs the bridge. Runner may be nil for callers using only Session openings.
func New(sandboxes SandboxGetter, runner RunnerProxy, dispatcher Dispatcher) (*RunnerBridge, error) {
	if sandboxes == nil || dispatcher == nil {
		return nil, errors.New("Sandbox getter and Session Dispatcher are required")
	}
	return &RunnerBridge{sandboxes: sandboxes, runner: runner, dispatcher: dispatcher}, nil
}

// OpenSession queues one immutable Session specification. The caller-minted
// UUID is the dispatcher's idempotency key, so a timeout is returned with that
// same ID for reconciliation or retry.
func (b *RunnerBridge) OpenSession(
	ctx context.Context, sandbox, sandboxUID string, sessionID uuid.UUID, spec *sessionpb.SessionSpec, setupScript *string,
) (*dispatcherpb.SessionOpening, error) {
	if sandbox == "" || sandboxUID == "" || spec == nil {
		return nil, errors.New("Sandbox name, UID and Session spec are required")
	}
	if setupScript != nil && utf8.RuneCountInString(*setupScript) > MaxSetupScriptCharacters {
		return nil, fmt.Errorf("setup script exceeds %d characters", MaxSetupScriptCharacters)
	}
	request := &dispatcherpb.OpenSessionRequest{
		Sandbox: sandbox, SandboxUid: sandboxUID, SessionId: sessionID.String(), Spec: proto.Clone(spec).(*sessionpb.SessionSpec),
	}
	if setupScript != nil {
		value := *setupScript
		request.SetupScript = &value
	}
	ctx, span := observability.Start(ctx, "agentplane.dispatcher.open_session")
	response, err := b.dispatcher.OpenSession(ctx, request)
	observability.End(span, err)
	if err != nil {
		var timeout *RequestTimeoutError
		if errors.As(err, &timeout) {
			return nil, &DispatcherRequestTimeoutError{SessionID: sessionID, Cause: err}
		}
		return nil, err
	}
	opening := response.GetOpening()
	if opening == nil || opening.GetSessionId() != sessionID.String() || opening.GetSandbox() != sandbox ||
		opening.GetSandboxUid() != sandboxUID {
		return nil, &SessionOpeningNotFoundError{SessionID: sessionID, Sandbox: sandbox}
	}
	return opening, nil
}

// SessionOpening looks up the durable opening and verifies both the returned
// Session ID and its named Sandbox before exposing it to the caller.
func (b *RunnerBridge) SessionOpening(ctx context.Context, sandbox string, sessionID uuid.UUID) (*dispatcherpb.SessionOpening, error) {
	if sandbox == "" {
		return nil, errors.New("Sandbox name is required")
	}
	response, err := b.dispatcher.GetSessionOpening(ctx, &dispatcherpb.GetSessionOpeningRequest{SessionId: sessionID.String()})
	if err != nil {
		return nil, err
	}
	opening := response.GetOpening()
	if opening == nil || opening.GetSessionId() != sessionID.String() || opening.GetSandbox() != sandbox {
		return nil, &SessionOpeningNotFoundError{SessionID: sessionID, Sandbox: sandbox}
	}
	return opening, nil
}

// ListSessions proxies the live runner's durable Session inventory through its
// current Sandbox incarnation.
func (b *RunnerBridge) ListSessions(ctx context.Context, sandboxName string) ([]*sessionpb.SessionSummary, error) {
	if b.runner == nil {
		return nil, errors.New("Runner proxy is not configured")
	}
	sandbox, err := b.currentSandbox(ctx, sandboxName)
	if err != nil {
		return nil, err
	}
	response, err := b.runner.ListSessions(ctx, sandbox)
	if err != nil {
		return nil, err
	}
	return response.GetSessions(), nil
}

// EnvironmentRuntimeStatus returns the live status snapshot for the current Sandbox
// incarnation. The snapshot is informational and does not wake the Sandbox.
func (b *RunnerBridge) EnvironmentRuntimeStatus(ctx context.Context, sandboxName string) (*sessionpb.EnvironmentRuntimeStatus, error) {
	if b.runner == nil {
		return nil, errors.New("Runner proxy is not configured")
	}
	sandbox, err := b.currentSandbox(ctx, sandboxName)
	if err != nil {
		return nil, err
	}
	return b.runner.EnvironmentRuntimeStatus(ctx, sandbox)
}

func (b *RunnerBridge) currentSandbox(ctx context.Context, name string) (*sandboxespb.Sandbox, error) {
	if name == "" {
		return nil, errors.New("Sandbox name is required")
	}
	sandbox, err := b.sandboxes.Get(ctx, name)
	if err != nil {
		return nil, err
	}
	if sandbox == nil || sandbox.GetName() != name || sandbox.GetUid() == "" || sandbox.GetNamespace() == "" {
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
	Namespace      string
	TokenFile      string
	Timeout        time.Duration
	TargetTemplate string
}

const MaxSetupScriptCharacters = 65_536
