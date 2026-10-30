package centralsampler

import (
	"encoding/json"
	"errors"
	"fmt"
	"time"

	runnerpb "github.com/agentydragon/ducktape/agentplane/proto/runner"
	samplerpb "github.com/agentydragon/ducktape/agentplane/x/central_sampler/samplerpb"
)

// Owner fences every runtime write to the lease generation that acquired it.
type Owner struct {
	ConversationID string
	Generation     int
	Token          string
}

// Conversation is the durable provider conversation and its current execution fence.
type Conversation struct {
	ID              string
	Generation      int
	Owner           *string
	LeaseUntil      *time.Time
	API             APIStyle
	Archive         NativeContext
	Pending         *PendingIntent
	Stopped         bool
	ExecutorSession *string
	ActiveContext   NativeContext
	Instructions    *string
	ContextTokens   *int
	// ExecutorShell is the shell name the bound exec-server last reported.
	ExecutorShell *string
	// EnvironmentContext is the last model-visible environment context.
	EnvironmentContext *EnvironmentContext
}

func (c *Conversation) Context() NativeContext {
	if c.ActiveContext == nil {
		return c.Archive
	}
	return c.ActiveContext
}

func (c *Conversation) apply(change func(NativeContext) error) error {
	if err := change(c.Archive); err != nil {
		return err
	}
	if c.ActiveContext != nil {
		return change(c.ActiveContext)
	}
	return nil
}

func (c *Conversation) appendToolResult(call ToolInvocation, output []ContentPart) error {
	// The archive ordinal is authoritative. Compacted active context has different local ordinals.
	original, err := c.Archive.FindCall(call.CallID)
	if err != nil {
		return err
	}
	if original.Encoding != call.Encoding || original.Name != call.Name || original.Input != call.Input ||
		(call.SourceRecord != 0 && original.SourceRecord != call.SourceRecord) {
		return ErrInvalid
	}
	return c.apply(func(state NativeContext) error {
		local, err := state.FindCall(call.CallID)
		if err != nil {
			return err
		}
		if local.Encoding != call.Encoding || local.Name != call.Name || local.Input != call.Input {
			return ErrInvalid
		}
		return state.AppendToolResult(local, output)
	})
}

type PendingIntentKind string

const (
	PendingIntentCell       PendingIntentKind = "cell"
	PendingIntentSampling   PendingIntentKind = "sampling"
	PendingIntentCompaction PendingIntentKind = "compaction"
	PendingIntentInterrupt  PendingIntentKind = "interrupt"
)

func (kind PendingIntentKind) valid() bool {
	switch kind {
	case PendingIntentCell, PendingIntentSampling, PendingIntentCompaction, PendingIntentInterrupt:
		return true
	default:
		return false
	}
}

func (kind *PendingIntentKind) UnmarshalJSON(data []byte) error {
	var value string
	if err := json.Unmarshal(data, &value); err != nil {
		return err
	}
	candidate := PendingIntentKind(value)
	if !candidate.valid() {
		return fmt.Errorf("unknown pending intent kind %q", value)
	}
	*kind = candidate
	return nil
}

type PendingIntent struct {
	ID   string            `json:"id"`
	Kind PendingIntentKind `json:"kind"`
}

func (pending PendingIntent) Validate() error {
	if pending.ID == "" || !pending.Kind.valid() {
		return errors.New("pending intent requires a known kind and ID")
	}
	return nil
}

type SessionAttribution struct {
	SessionID string
	ThreadID  string
	TurnID    string
}

// Session stores the immutable launch request plus the event and command cursors.
type Session struct {
	ID                string
	ConversationID    string
	AccountUID        string
	Configuration     []byte
	Model             string
	Effort            string
	SourceID          string
	Cursor            uint64
	ActiveTurn        *string
	ActiveCommand     *string
	WorkspacePrepared bool
	SetupState        runnerpb.SetupState
	SetupStarted      bool
	Blocked           bool
	BlockedReason     *string
}

// Intent is a committed remote-effect request and its acknowledged result, if known.
type Intent struct {
	ID             string
	ConversationID string
	Generation     int
	Request        map[string]any
	Result         map[string]any
}

// Command is an admitted session command and its durable execution status.
type Command struct {
	SessionID     string
	ID            string
	Payload       []byte
	Admission     uint64
	Outcome       samplerpb.CommandOutcome
	FailureReason *string
}

// RemoteProcess retains unread output until the model consumes it.
type RemoteProcess struct {
	ID             string
	ConversationID string
	Generation     int
	AfterSequence  *uint64
	Status         string
	TTY            bool
	ExitCode       *int
	// BufferedOutput is the retained head and tail of unread output; OmittedBytes were dropped
	// between them.
	BufferedOutput []byte
	OmittedBytes   int
	// ModelSessionID is the unified exec session number the model uses; absent for setup.
	ModelSessionID *int
}

// LiveCell is a JavaScript cell whose result has not yet been durably recorded.
type LiveCell struct {
	ID             string
	ConversationID string
	Generation     int
	Status         string
}

// ExecutionState is the set of remote handles whose effects may still be live.
type ExecutionState struct {
	Cells     []LiveCell
	Processes []RemoteProcess
}

// InterruptRequest is scoped to one lease generation and, for session commands, one turn.
type InterruptRequest struct {
	ID             string
	ConversationID string
	Generation     int
	TargetTurn     *string
	Result         map[string]any
}

// Errors are distinguished by callers at the service boundary.
var (
	ErrConflict     = errors.New("resource conflicts with an existing request")
	ErrMissing      = errors.New("resource not found")
	ErrForbidden    = errors.New("resource belongs to another account")
	ErrPrecondition = errors.New("resource is not in a state that permits this operation")
	ErrInvalid      = errors.New("request is invalid")
)

type LostOwnershipError struct{ Reason string }

func (e *LostOwnershipError) Error() string {
	if e.Reason == "" {
		return "sampler generation is stale or expired"
	}
	return e.Reason
}

type UnknownEffectError struct{ Reason string }

func (e *UnknownEffectError) Error() string {
	if e.Reason == "" {
		return "remote effect outcome is unknown; automatic replay is unsafe"
	}
	return e.Reason
}

type InterruptPendingError struct{}

func (InterruptPendingError) Error() string {
	return "an interrupt is pending; the next dispatch is blocked"
}

// KnownFailureError records a provider or pre-dispatch rejection whose effect is known.
type KnownFailureError struct {
	Evidence map[string]any
	Message  string
}

func (e *KnownFailureError) Error() string {
	if e.Message != "" {
		return e.Message
	}
	return fmt.Sprint(e.Evidence)
}
