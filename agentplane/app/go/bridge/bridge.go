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
	runnerpb "github.com/agentydragon/ducktape/agentplane/proto/runner"
	sandboxespb "github.com/agentydragon/ducktape/agentplane/proto/sandboxes"
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
	SubmitInput(context.Context, *dispatcherpb.SubmitInputRequest) (*dispatcherpb.SubmitInputResponse, error)
	ChangeModel(context.Context, *dispatcherpb.ChangeModelRequest) (*dispatcherpb.ChangeModelResponse, error)
	ChangeReasoningEffort(context.Context, *dispatcherpb.ChangeReasoningEffortRequest) (*dispatcherpb.ChangeReasoningEffortResponse, error)
	InterruptTurn(context.Context, *dispatcherpb.InterruptTurnRequest) (*dispatcherpb.InterruptTurnResponse, error)
	OpenSession(context.Context, *dispatcherpb.OpenSessionRequest) (*dispatcherpb.OpenSessionResponse, error)
	GetSessionOpening(context.Context, *dispatcherpb.GetSessionOpeningRequest) (*dispatcherpb.GetSessionOpeningResponse, error)
}

// RunnerProxy performs informational calls through the Sandbox's control
// Service. The concrete GatewayRunnerProxy binds all four identity values on
// every call.
type RunnerProxy interface {
	ListSessions(context.Context, *sandboxespb.Sandbox) (*runnerpb.ListSessionsResponse, error)
	RunnerStatus(context.Context, *sandboxespb.Sandbox) (*runnerpb.RunnerStatus, error)
}

// Readiness supplies the app-owned Thread check and History Service feed state
// used before admitting a new command.
type Readiness interface {
	Exists(context.Context, uuid.UUID) (bool, error)
	FeedState(context.Context, uuid.UUID) (*FeedSnapshot, error)
}

// FeedSnapshot is the admission-relevant part of the History Service's folded
// feed state. A nil snapshot or nil End is healthy, matching the Python bridge.
type FeedSnapshot struct{ End *FeedError }

// FeedError means History rejected the runner event feed.
type FeedError struct{ Message string }

// RunnerBridge joins the app's sandbox identity, dispatcher, runner, and thread
// readiness services without owning HTTP routes or runtime startup.
type RunnerBridge struct {
	sandboxes  SandboxGetter
	runner     RunnerProxy
	dispatcher Dispatcher
	readiness  Readiness
}

// New constructs the bridge. The runner and readiness services may be nil when
// a caller only uses the dispatcher methods, but SandboxGetter and Dispatcher
// are always required.
func New(sandboxes SandboxGetter, runner RunnerProxy, dispatcher Dispatcher, readiness Readiness) (*RunnerBridge, error) {
	if sandboxes == nil || dispatcher == nil {
		return nil, errors.New("Sandbox getter and Session Dispatcher are required")
	}
	return &RunnerBridge{sandboxes: sandboxes, runner: runner, dispatcher: dispatcher, readiness: readiness}, nil
}

// OpenSession queues one immutable Session specification. The caller-minted
// UUID is the dispatcher's idempotency key, so a timeout is returned with that
// same ID for reconciliation or retry.
func (b *RunnerBridge) OpenSession(ctx context.Context, sandbox string, sessionID uuid.UUID, spec *runnerpb.SessionSpec, setupScript *string) (*dispatcherpb.SessionOpening, error) {
	if sandbox == "" || spec == nil {
		return nil, errors.New("Sandbox name and Session spec are required")
	}
	if setupScript != nil && utf8.RuneCountInString(*setupScript) > MaxSetupScriptCharacters {
		return nil, fmt.Errorf("setup script exceeds %d characters", MaxSetupScriptCharacters)
	}
	request := &dispatcherpb.OpenSessionRequest{Sandbox: sandbox, SessionId: sessionID.String(), Spec: proto.Clone(spec).(*runnerpb.SessionSpec)}
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
	if opening == nil || opening.GetSessionId() != sessionID.String() || opening.GetSandbox() != sandbox {
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

// SubmitInput first checks app Thread/History readiness, then durably admits
// one idempotent input command through the Session Dispatcher.
func (b *RunnerBridge) SubmitInput(ctx context.Context, sandbox string, sessionID uuid.UUID, commandID, text string) (*dispatcherpb.Dispatch, error) {
	if err := b.commandRequestReady(ctx, sandbox, sessionID, commandID); err != nil {
		return nil, err
	}
	response, err := b.dispatcher.SubmitInput(ctx, &dispatcherpb.SubmitInputRequest{
		CommandId: commandID, SessionId: sessionID.String(), Sandbox: sandbox, Text: text,
	})
	if err != nil {
		return nil, wrapCommandTimeout(commandID, err)
	}
	if response.GetDispatch() == nil {
		return nil, errors.New("Session Dispatcher returned no Dispatch for SubmitInput")
	}
	return response.GetDispatch(), nil
}

// ChangeModel checks readiness, then durably admits a model-change command.
func (b *RunnerBridge) ChangeModel(ctx context.Context, sandbox string, sessionID uuid.UUID, commandID, model string) (*dispatcherpb.Dispatch, error) {
	if err := b.commandRequestReady(ctx, sandbox, sessionID, commandID); err != nil {
		return nil, err
	}
	response, err := b.dispatcher.ChangeModel(ctx, &dispatcherpb.ChangeModelRequest{
		CommandId: commandID, SessionId: sessionID.String(), Sandbox: sandbox, Model: model,
	})
	if err != nil {
		return nil, wrapCommandTimeout(commandID, err)
	}
	if response.GetDispatch() == nil {
		return nil, errors.New("Session Dispatcher returned no Dispatch for ChangeModel")
	}
	return response.GetDispatch(), nil
}

// ChangeReasoningEffort checks readiness, then durably admits a reasoning
// effort change.
func (b *RunnerBridge) ChangeReasoningEffort(ctx context.Context, sandbox string, sessionID uuid.UUID, commandID, effort string) (*dispatcherpb.Dispatch, error) {
	if err := b.commandRequestReady(ctx, sandbox, sessionID, commandID); err != nil {
		return nil, err
	}
	response, err := b.dispatcher.ChangeReasoningEffort(ctx, &dispatcherpb.ChangeReasoningEffortRequest{
		CommandId: commandID, SessionId: sessionID.String(), Sandbox: sandbox, Effort: effort,
	})
	if err != nil {
		return nil, wrapCommandTimeout(commandID, err)
	}
	if response.GetDispatch() == nil {
		return nil, errors.New("Session Dispatcher returned no Dispatch for ChangeReasoningEffort")
	}
	return response.GetDispatch(), nil
}

// InterruptTurn checks readiness and asks the Dispatcher to interrupt only the
// caller's identified active turn.
func (b *RunnerBridge) InterruptTurn(ctx context.Context, sandbox string, sessionID uuid.UUID, commandID, turnID string) (*dispatcherpb.InterruptTurnResponse, error) {
	if err := b.commandRequestReady(ctx, sandbox, sessionID, commandID); err != nil {
		return nil, err
	}
	response, err := b.dispatcher.InterruptTurn(ctx, &dispatcherpb.InterruptTurnRequest{
		CommandId: commandID, SessionId: sessionID.String(), Sandbox: sandbox, TurnId: turnID,
	})
	if err != nil {
		return nil, wrapCommandTimeout(commandID, err)
	}
	if response == nil {
		return nil, errors.New("Session Dispatcher returned no result for InterruptTurn")
	}
	return response, nil
}

func (b *RunnerBridge) commandRequestReady(ctx context.Context, sandbox string, sessionID uuid.UUID, commandID string) error {
	if sandbox == "" || commandID == "" {
		return errors.New("Sandbox name and command ID are required")
	}
	return b.CommandReady(ctx, sessionID)
}

func wrapCommandTimeout(commandID string, err error) error {
	var timeout *RequestTimeoutError
	if errors.As(err, &timeout) {
		return &DispatcherRequestTimeoutError{CommandID: commandID, Cause: err}
	}
	return err
}

// ListSessions proxies the live runner's durable Session inventory through its
// current Sandbox incarnation.
func (b *RunnerBridge) ListSessions(ctx context.Context, sandboxName string) ([]*runnerpb.SessionSummary, error) {
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

// RunnerStatus returns the live status snapshot for the current Sandbox
// incarnation. The snapshot is informational and does not wake the Sandbox.
func (b *RunnerBridge) RunnerStatus(ctx context.Context, sandboxName string) (*runnerpb.RunnerStatus, error) {
	if b.runner == nil {
		return nil, errors.New("Runner proxy is not configured")
	}
	sandbox, err := b.currentSandbox(ctx, sandboxName)
	if err != nil {
		return nil, err
	}
	return b.runner.RunnerStatus(ctx, sandbox)
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

// CommandReady rejects an absent Thread or a feed that History has marked as
// rejected before a caller submits another command to the Dispatcher.
func (b *RunnerBridge) CommandReady(ctx context.Context, threadID uuid.UUID) error {
	if b.readiness == nil {
		return errors.New("Thread readiness is not configured")
	}
	exists, err := b.readiness.Exists(ctx, threadID)
	if err != nil {
		return err
	}
	if !exists {
		return &ThreadNotFoundError{ThreadID: threadID}
	}
	snapshot, err := b.readiness.FeedState(ctx, threadID)
	if err != nil {
		return err
	}
	if snapshot != nil && snapshot.End != nil {
		return &ThreadStateError{Message: "runner history is rejected: " + snapshot.End.Message}
	}
	return nil
}

// ParseSessionSpec applies strict proto-JSON fields over a cloned set of
// defaults. Explicit empty values override defaults, and unknown fields fail.
func ParseSessionSpec(defaults *runnerpb.SessionSpec, fields map[string]any) (*runnerpb.SessionSpec, error) {
	if defaults == nil {
		defaults = &runnerpb.SessionSpec{}
	}
	merged := proto.Clone(defaults).(*runnerpb.SessionSpec)
	encoded, err := json.Marshal(fields)
	if err != nil {
		return nil, &MalformedMessageError{Cause: err}
	}
	partial := &runnerpb.SessionSpec{}
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
